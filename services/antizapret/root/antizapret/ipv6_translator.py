#!/usr/bin/env -S python3 -u
# -*- coding: utf-8 -*-
"""
IPv4 → IPv6 translator for AntiZapret VPN.

Accepts IPv4 connections (redirected via iptables DNAT) destined for
virtual IPv4 addresses, looks up the corresponding real IPv6 address,
and relays traffic to the IPv6 destination.

Used for selective IPv6 egress of configured domains in Singlebox mode.

Traffic flow:
    Client → virtual IPv4 (via VPN tunnel)
        → iptables DNAT to 127.0.0.1:TRANSLATOR_PORT
        → this translator (gets original dst via SO_ORIGINAL_DST)
        → looks up mapping: virtual IPv4 → real IPv6
        → connects to [real IPv6]:port
        → relays TCP/UDP bidirectionally

Mapping file format (written by dnsmap.py):
    <fake_ipv4> <real_ipv6>
    Example: 14.16.50.1 2001:db8::1
"""

from __future__ import print_function

import atexit
import os
import signal
import socket
import struct
import sys
import threading
import time

# SO_ORIGINAL_DST (value 80) is a getsockopt() option used on a connected
# socket to retrieve the original destination of a redirected connection.
# IP_ORIGDSTADDR / IP_RECVORIGDSTADDR (value 20) is the setsockopt() option
# used to receive the original destination as ancillary data via recvmsg().
# They are distinct Linux socket options and must not share a value.
SO_ORIGINAL_DST = 80
IP_RECVORIGDSTADDR = 20
SOL_IP = 0

MAPPING_FILE = '/root/antizapret/result/ipv6-egress-mappings.txt'
LISTEN_ADDR = '127.0.0.1'
LISTEN_PORT = int(os.getenv('IPV6_TRANSLATOR_PORT', '9000'))
RELOAD_INTERVAL = 5  # seconds between mapping file reloads
CONNECT_TIMEOUT = 10  # seconds for IPv6 connect
RELAY_BUFFER_SIZE = 65536
UDP_UPSTREAM_IDLE_TIMEOUT = 300  # seconds before closing idle upstream sockets

# Optional diagnostic mode. When enabled, only technical connection events are
# logged. Payloads, SNI and any user data are never inspected or logged.
DEBUG = os.getenv('IPV6_TRANSLATOR_DEBUG', '').lower() in ('1', 'true', 'yes', 'on')

# ---------------------------------------------------------------------------
# Mapping table
# ---------------------------------------------------------------------------

class MappingTable:
    """Thread-safe mapping of fake IPv4 → real IPv6."""

    def __init__(self, path):
        self.path = path
        self._mappings = {}  # fake_ipv4_str -> real_ipv6_str
        self._lock = threading.RLock()
        self._load()

    def _load(self):
        """Load mappings from file."""
        new_mappings = {}
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split()
                    if len(parts) >= 2:
                        fake_ipv4 = parts[0]
                        real_ipv6 = parts[1]
                        new_mappings[fake_ipv4] = real_ipv6
        except FileNotFoundError:
            pass  # No mappings yet
        except OSError as error:
            print('translator: failed to read mapping file: {}'.format(error))

        with self._lock:
            self._mappings = new_mappings
        print('translator: loaded {} mapping(s) from {}'.format(
            len(new_mappings), self.path
        ))

    def lookup(self, fake_ipv4):
        """Return real IPv6 for a fake IPv4 address, or None."""
        with self._lock:
            return self._mappings.get(fake_ipv4)

    def start_watcher(self, interval=RELOAD_INTERVAL):
        """Start a background thread that periodically reloads the mapping file."""
        def _watch():
            while True:
                time.sleep(interval)
                try:
                    mtime = os.path.getmtime(self.path)
                except OSError:
                    continue
                # Simple approach: always reload (file is small)
                self._load()

        t = threading.Thread(target=_watch, daemon=True)
        t.start()
        return t


# ---------------------------------------------------------------------------
# TCP relay
# ---------------------------------------------------------------------------

def parse_original_destination(data):
    """Parse IPv4 sockaddr returned by SO_ORIGINAL_DST."""
    if len(data) < 8:
        raise struct.error(
            "SO_ORIGINAL_DST returned {} bytes; expected at least 8".format(
                len(data)
            )
        )
    port = struct.unpack("!H", data[2:4])[0]
    ip = socket.inet_ntoa(data[4:8])
    return ip, port


def relay_tcp(src_sock, dst_sock):
    """Relay TCP bidirectionally and transparently with proper half-close.

    No payload inspection, buffering or modification happens on the normal
    path. TLS payloads and SNI are forwarded byte-for-byte; no diagnostic
    inspection is enabled unless IPV6_TRANSLATOR_DEBUG is set.

    Half-close coordination: when one direction reaches EOF, the write side of
    the opposite socket is shut down (SHUT_WR) so the remote peer learns no
    more data is coming on that direction, while the socket stays open for the
    reverse direction to finish delivering data. Both sockets are closed only
    after both relay threads have finished, so an outgoing socket is never
    closed while the reverse direction still needs to transfer data. On a real
    socket error both directions are aborted (SHUT_RDWR) to avoid hanging.
    """
    state = {'errors': 0}
    state_lock = threading.Lock()

    def _forward(src, dst):
        try:
            while True:
                data = src.recv(RELAY_BUFFER_SIZE)
                if not data:
                    # Local side done writing to dst; tell dst peer no more
                    # data is coming on this direction. Keep dst open so the
                    # reverse direction can still deliver its remaining data.
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                dst.sendall(data)
        except (OSError, ConnectionError):
            with state_lock:
                state['errors'] += 1
            # Abort both directions on error so the other thread does not hang.
            try:
                dst.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    t1 = threading.Thread(target=_forward, args=(src_sock, dst_sock), daemon=True)
    t2 = threading.Thread(target=_forward, args=(dst_sock, src_sock), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Both directions finished: release the sockets exactly once.
    try:
        src_sock.close()
    except OSError:
        pass
    try:
        dst_sock.close()
    except OSError:
        pass


def handle_tcp_connection(conn, addr, mapping_table):
    """Handle a single TCP connection from the iptables DNAT redirect."""
    try:
        # Get the original destination address before DNAT
        try:
            orig_dst = conn.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
            orig_dst_ip, orig_dst_port = parse_original_destination(orig_dst)
        except (OSError, struct.error) as error:
            print('translator: failed to get original destination: {}'.format(error))
            conn.close()
            return

        print('translator: TCP connection from {}:{} → original dst {}:{}'.format(
            addr[0], addr[1], orig_dst_ip, orig_dst_port
        ))

        # Look up the real IPv6 address
        real_ipv6 = mapping_table.lookup(orig_dst_ip)
        if not real_ipv6:
            print('translator: no mapping for {} (closing)'.format(orig_dst_ip))
            conn.close()
            return

        print('translator: mapping {} → {} (port {})'.format(
            orig_dst_ip, real_ipv6, orig_dst_port
        ))

        # Connect to the IPv6 destination
        try:
            dst_sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            dst_sock.settimeout(CONNECT_TIMEOUT)
            dst_sock.connect((real_ipv6, orig_dst_port, 0, 0))
            dst_sock.settimeout(None)
        except (OSError, ConnectionError) as error:
            print('translator: failed to connect to [{}]:{}: {}'.format(
                real_ipv6, orig_dst_port, error
            ))
            conn.close()
            return

        print('translator: connected to [{}]:{}'.format(real_ipv6, orig_dst_port))

        # Relay traffic bidirectionally
        relay_tcp(conn, dst_sock)
        print('translator: TCP relay ended for {} → [{}]'.format(
            orig_dst_ip, real_ipv6
        ))

    except Exception as error:
        print('translator: unexpected error handling TCP connection: {}'.format(error))
        try:
            conn.close()
        except OSError:
            pass


def run_tcp_server(listen_addr, listen_port, mapping_table):
    """Run the TCP translator server."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((listen_addr, listen_port))
    server.listen(128)
    print('translator: listening on {}:{} (TCP)'.format(listen_addr, listen_port))

    while True:
        try:
            conn, addr = server.accept()
            t = threading.Thread(
                target=handle_tcp_connection,
                args=(conn, addr, mapping_table),
                daemon=True
            )
            t.start()
        except OSError as error:
            print('translator: accept error: {}'.format(error))
            break


# ---------------------------------------------------------------------------
# UDP upstream socket pool (for QUIC/HTTP/3 compatibility)
# ---------------------------------------------------------------------------

class UpstreamUDPSocket:
    """A UDP socket connected to an IPv6 destination, reused for multiple packets."""

    def __init__(self, real_ipv6, port):
        self.real_ipv6 = real_ipv6
        self.port = port
        self.sock = None
        self.last_used = time.time()
        self._create()

    def _create(self):
        try:
            self.sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            self.sock.settimeout(CONNECT_TIMEOUT)
            self.sock.connect((self.real_ipv6, self.port, 0, 0))
            self.sock.settimeout(None)
            self.last_used = time.time()
        except OSError as error:
            print('translator: failed to create upstream UDP socket to [{}]:{}: {}'.format(
                self.real_ipv6, self.port, error
            ))
            self.sock = None

    def send(self, data):
        """Send data to the IPv6 destination."""
        if self.sock is None:
            self._create()
        if self.sock is None:
            return False
        try:
            self.sock.send(data)
            self.last_used = time.time()
            return True
        except OSError as error:
            print('translator: failed to send UDP to [{}]:{}: {}'.format(
                self.real_ipv6, self.port, error
            ))
            self.close()
            return False

    def recv(self, size):
        """Receive response from the IPv6 destination."""
        if self.sock is None:
            return None
        try:
            self.sock.settimeout(5.0)
            data, _ = self.sock.recvfrom(size)
            self.sock.settimeout(None)
            self.last_used = time.time()
            return data
        except socket.timeout:
            print('translator: UDP timeout for [{}]:{}'.format(self.real_ipv6, self.port))
            return None
        except OSError as error:
            print('translator: UDP receive error: {}'.format(error))
            self.close()
            return None

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def is_idle(self):
        return time.time() - self.last_used > UDP_UPSTREAM_IDLE_TIMEOUT


class UpstreamUDPSocketPool:
    """Pool of upstream UDP sockets keyed by (client_ip, client_port, fake_ipv4, port).

    Each client flow gets its own upstream socket to prevent response mixing.
    This is essential for QUIC/HTTP/3 where multiple clients may share the same
    destination address:port but must have separate connection state.
    """

    def __init__(self):
        self._pool = {}  # (client_ip, client_port, fake_ipv4, port) -> UpstreamUDPSocket
        self._lock = threading.RLock()

    def get(self, client_ip, client_port, fake_ipv4, port, real_ipv6):
        """Get or create an upstream socket for the given client flow.

        Key includes client address so that multiple clients sending to the same
        virtual_ipv4:port do not share an upstream socket and their responses
        are not mixed up.
        """
        key = (client_ip, client_port, fake_ipv4, port)
        with self._lock:
            existing = self._pool.get(key)
            if existing is not None and existing.real_ipv6 == real_ipv6:
                return existing
            if existing is not None:
                existing.close()
            upstream = UpstreamUDPSocket(real_ipv6, port)
            self._pool[key] = upstream
            return upstream

    def remove(self, client_ip, client_port, fake_ipv4, port):
        """Remove an upstream socket from the pool."""
        key = (client_ip, client_port, fake_ipv4, port)
        with self._lock:
            upstream = self._pool.pop(key, None)
            if upstream is not None:
                upstream.close()

    def cleanup_idle(self):
        """Close idle upstream sockets."""
        with self._lock:
            idle_keys = [
                key for key, upstream in self._pool.items()
                if upstream.is_idle()
            ]
            for key in idle_keys:
                upstream = self._pool.pop(key, None)
                if upstream is not None:
                    upstream.close()
                    print('translator: closed idle upstream UDP socket for {}'.format(key))


# ---------------------------------------------------------------------------
# UDP relay
# ---------------------------------------------------------------------------

def extract_original_dst_from_cmsg(msg):
    """Extract original destination address from recvmsg() ancillary data.

    Returns (orig_dst_ip, orig_dst_port) or (None, None) on failure.
    """
    try:
        for cmsg_level, cmsg_type, cmsg_data in msg[1]:
            if cmsg_level == SOL_IP and cmsg_type == IP_RECVORIGDSTADDR:
                # cmsg_data is a struct sockaddr_in (16 bytes) per Linux ABI:
                #   sin_family (2 bytes, HOST byte order)
                #   sin_port (2 bytes, NETWORK byte order)
                #   sin_addr (4 bytes, NETWORK byte order)
                #   sin_zero (8 bytes padding)
                # sin_family is host byte order, unlike sin_port/sin_addr.
                if len(cmsg_data) >= 8:
                    family = struct.unpack('H', cmsg_data[0:2])[0]
                    if family == socket.AF_INET:
                        port = struct.unpack('!H', cmsg_data[2:4])[0]
                        ip = socket.inet_ntoa(cmsg_data[4:8])
                        return ip, port
    except (struct.error, OSError, IndexError):
        pass
    return None, None


def handle_udp_packet(data, src_addr, orig_dst_ip, orig_dst_port, mapping_table, upstream_pool, listen_sock):
    """Handle a single UDP packet.

    orig_dst_ip and orig_dst_port are obtained via IP_RECVORIGDSTADDR ancillary data,
    which reliably returns the original destination before DNAT redirected the packet.
    """
    try:
        # Look up the real IPv6 address
        real_ipv6 = mapping_table.lookup(orig_dst_ip)
        if not real_ipv6:
            print('translator: no UDP mapping for {} (port {})'.format(orig_dst_ip, orig_dst_port))
            return

        client_ip = src_addr[0]
        client_port = src_addr[1]

        print('translator: UDP packet from {}:{} → orig dst {}:{} → IPv6 [{}]'.format(
            client_ip, client_port, orig_dst_ip, orig_dst_port, real_ipv6
        ))

        # Get or create upstream socket for this specific client flow.
        # Key includes client address so responses are not mixed between clients.
        upstream = upstream_pool.get(client_ip, client_port, orig_dst_ip, orig_dst_port, real_ipv6)
        if upstream is None or upstream.sock is None:
            print('translator: failed to create upstream socket for [{}]:{}'.format(
                real_ipv6, orig_dst_port
            ))
            return

        # Send the packet
        if not upstream.send(data):
            upstream_pool.remove(client_ip, client_port, orig_dst_ip, orig_dst_port)
            return

        # Wait for response
        response = upstream.recv(RELAY_BUFFER_SIZE)
        if response is None:
            upstream_pool.remove(client_ip, client_port, orig_dst_ip, orig_dst_port)
            return

        # Send response back to the originating client
        try:
            listen_sock.sendto(response, src_addr)
        except OSError as error:
            print('translator: failed to send response to {}:{} : {}'.format(
                client_ip, client_port, error
            ))

    except Exception as error:
        print('translator: unexpected error handling UDP packet: {}'.format(error))


def run_udp_server(listen_addr, listen_port, mapping_table, upstream_pool):
    """Run the UDP translator server.

    Uses IP_ORIGDSTADDR (IP_RECVORIGDSTADDR) ancillary data via recvmsg() to
    recover the original destination address before DNAT redirected the packet
    to this translator. This is the only reliable way to determine the original
    destination of each individual UDP datagram on an unconnected listening
    socket.

    If the socket option is unavailable (e.g. restricted kernel/container),
    per-datagram original destination cannot be determined reliably:
    getsockopt(SO_ORIGINAL_DST) on an unconnected UDP socket is not a
    per-datagram mechanism and must not be used to forward packets to a guessed
    address. In that case every datagram is dropped with a one-time diagnostic
    instead of being redirected to the wrong peer.
    """
    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Enable receiving original destination address as ancillary data
    recv_orig_dst_available = True
    try:
        listen_sock.setsockopt(SOL_IP, IP_RECVORIGDSTADDR, 1)
    except OSError as error:
        recv_orig_dst_available = False
        print('translator: IP_ORIGDSTADDR/IP_RECVORIGDSTADDR unavailable: {} '
              '(UDP original destination cannot be determined per datagram; '
              'UDP packets will be dropped)'.format(error))

    listen_sock.bind((listen_addr, listen_port))
    print('translator: listening on {}:{} (UDP)'.format(listen_addr, listen_port))

    _orig_dst_unavailable_warned = False

    while True:
        try:
            # recvmsg() returns: (data, ancdata, flags, address)
            data, ancdata, flags, src_addr = listen_sock.recvmsg(
                RELAY_BUFFER_SIZE,
                socket.CMSG_SPACE(16)  # Space for sockaddr_in ancillary data
            )

            # Extract original destination from ancillary data
            orig_dst_ip, orig_dst_port = extract_original_dst_from_cmsg(
                (data, ancdata, flags, src_addr)
            )

            if orig_dst_ip is None or orig_dst_port is None:
                # Original destination could not be determined reliably.
                # Drop the datagram rather than forwarding to a wrong peer.
                if not recv_orig_dst_available and not _orig_dst_unavailable_warned:
                    _orig_dst_unavailable_warned = True
                    print('translator: IP_ORIGDSTADDR unavailable; cannot determine '
                          'original destination per UDP packet; dropping UDP packets')
                continue

            t = threading.Thread(
                target=handle_udp_packet,
                args=(data, src_addr, orig_dst_ip, orig_dst_port, mapping_table, upstream_pool, listen_sock),
                daemon=True
            )
            t.start()
        except OSError as error:
            print('translator: UDP receive error: {}'.format(error))
            break


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    listen_port = int(os.getenv('IPV6_TRANSLATOR_PORT', str(LISTEN_PORT)))
    mapping_file = os.getenv('IPV6_MAPPING_FILE', MAPPING_FILE)

    print('translator: starting IPv4→IPv6 translator')
    print('translator: listen address: {}:{}'.format(LISTEN_ADDR, listen_port))
    print('translator: mapping file: {}'.format(mapping_file))
    if DEBUG:
        print('translator: diagnostic mode enabled')

    mapping_table = MappingTable(mapping_file)
    watcher = mapping_table.start_watcher()
    upstream_pool = UpstreamUDPSocketPool()

    # Start TCP server in a separate thread
    tcp_thread = threading.Thread(
        target=run_tcp_server,
        args=(LISTEN_ADDR, listen_port, mapping_table),
        daemon=True
    )
    tcp_thread.start()

    # Start UDP server in a separate thread
    udp_thread = threading.Thread(
        target=run_udp_server,
        args=(LISTEN_ADDR, listen_port, mapping_table, upstream_pool),
        daemon=True
    )
    udp_thread.start()

    # Start upstream socket cleanup thread
    def _cleanup_loop():
        while True:
            time.sleep(60)
            upstream_pool.cleanup_idle()

    cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
    cleanup_thread.start()

    # Handle signals for graceful shutdown
    def _shutdown(signum, frame):
        print('translator: shutting down...')
        upstream_pool = None
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Keep main thread alive
    while True:
        time.sleep(3600)


if __name__ == '__main__':
    main()
