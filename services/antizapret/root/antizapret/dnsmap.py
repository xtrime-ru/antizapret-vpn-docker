#!/usr/bin/env -S python3 -u
# -*- coding: utf-8 -*-

from __future__ import print_function

import atexit, base64, http.client, os, queue, re, signal, socket, struct, subprocess, threading, time, traceback
from urllib.parse import quote

from collections import deque, namedtuple
from copy import copy
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network

import idna
import maxminddb

from dnslib import DNSQuestion, DNSRecord, RCODE, QTYPE, A, AAAA, HTTPS, RR
from dnslib.server import DNSServer, DNSHandler, BaseResolver, DNSLogger


AsnMatchRules = namedtuple('AsnMatchRules', (
    'asn_rules', 'substring_rules', 'regex_rules', 'domain_rules'
), defaults=((),))
MappingRequest = namedtuple('MappingRequest', ('real_addr', 'fake_addr', 'event'))


# Result codes for the AdGuard az-ipv6-* IPv6 authorization check.
IPV6_EGRESS_AUTHORIZED = 1   # AdGuard explicitly authorized the domain for IPv6 egress
IPV6_EGRESS_DENIED = 2       # AdGuard denied (REFUSED/SERVFAIL catch-all)
IPV6_EGRESS_ERROR = 3        # Technical error (timeout, connection failure, ...)

# Comment marker used to persist SIIT (Tundra-NAT64) bindings in the dnsmap
# iptables chain:
#   -A dnsmap -d <vip> -m comment --comment "dnsmap:siit:v1:<ipv6>" -j RETURN
SIIT_COMMENT_PREFIX = 'dnsmap:siit:v1:'
MAX_SIIT_AAAA_RECORDS = 4

# ULA NAT64 client prefix used to represent an IPv4 client as an IPv6 address:
#   <client_ipv4> <-> fd64:ff9b::<client_ipv4_hex>
# Example: 10.1.166.5 <-> fd64:ff9b::a01:a605
CLIENT_IPV6_PREFIX = 'fd64:ff9b::'

# Client IPv6 prefix network (fd64:ff9b::/96) used to validate that
# addresses in Tundra requests belong to the client prefix.
CLIENT_IPV6_NETWORK = IPv6Network('fd64:ff9b::/96')

# Tundra-NAT64 service IPv4 address (router.ipv4 in tundra-nat64.conf).
# Used as fallback source for unknown IPv6 sources in ICMPv6 Packet Too Big
# translations, so the kernel accepts incoming ICMPv4 packets on the
# unnumbered tundra interface.
TUNDRA_ROUTER_IPV4 = '14.16.0.254'

# Cache lifetime (seconds) advertised in Tundra external translation responses.
TUNDRA_CACHE_LIFETIME = 60


# ------------------------------------------------------------------
# Tundra-NAT64 external address translation protocol (wireformat)
#
# Both request and response messages are exactly 40 bytes:
#   Byte 0:   Magic byte 0x54 ('T')
#   Byte 1:   Protocol version 1
#   Byte 2:   bit7=Response, bit6=Error, bit5=ICMP, bits4-0=Message type
#   Byte 3:   Cache lifetime (seconds)
#   Bytes 4-7: Message identifier (uint32 big-endian)
#   Bytes 8-23: Source IP address (16 bytes; IPv4 in first 4 bytes, rest zeroed)
#   Bytes 24-39: Destination IP address (16 bytes; IPv4 in first 4 bytes, rest zeroed)
#
# Message types:
#   1 = 4TO6_MAIN_PACKET, 2 = 4TO6_ICMP_ERROR_PACKET,
#   3 = 6TO4_MAIN_PACKET, 4 = 6TO4_ICMP_ERROR_PACKET
# ------------------------------------------------------------------

TUNDRA_MAGIC = 0x54
TUNDRA_VERSION = 1
TUNDRA_MSG_SIZE = 40

TUNDRA_MSG_TYPE_4TO6_MAIN = 1
TUNDRA_MSG_TYPE_4TO6_ICMP_ERROR = 2
TUNDRA_MSG_TYPE_6TO4_MAIN = 3
TUNDRA_MSG_TYPE_6TO4_ICMP_ERROR = 4

TUNDRA_BIT_RESPONSE = 0x80
TUNDRA_BIT_ERROR = 0x40
TUNDRA_BIT_ICMP = 0x20

TUNDRA_4TO6_TYPES = frozenset((TUNDRA_MSG_TYPE_4TO6_MAIN, TUNDRA_MSG_TYPE_4TO6_ICMP_ERROR))


def _tundra_ip_to_16_bytes(ip_address):
    """Render an IPv4 or IPv6 address (or its string form) as 16 bytes
    (IPv4 in first 4 bytes, rest zeroed)."""
    if ip_address is None:
        return b'\x00' * 16
    if isinstance(ip_address, str):
        try:
            ip_address = IPv4Address(ip_address)
        except ValueError:
            ip_address = IPv6Address(ip_address)
    if isinstance(ip_address, IPv4Address):
        return ip_address.packed + (b'\x00' * 12)
    return IPv6Address(ip_address).packed


def _tundra_16_bytes_to_ip(data, expect_ipv4):
    """Convert 16 bytes to an IPv4Address or IPv6Address."""
    if expect_ipv4:
        if data[4:] != b'\x00' * 12:
            raise ValueError('IPv4 field has nonzero trailing bytes')
        return IPv4Address(data[0:4])
    return IPv6Address(data)


def _tundra_encode_message(response, error, icmp, msg_type, cache_lifetime, msg_id, src_ip, dst_ip):
    """Encode a 40-byte Tundra external address translation message."""
    byte2 = msg_type
    if response:
        byte2 |= TUNDRA_BIT_RESPONSE
    if error:
        byte2 |= TUNDRA_BIT_ERROR
    if icmp:
        byte2 |= TUNDRA_BIT_ICMP
    header = struct.pack('!cBBBI', bytes([TUNDRA_MAGIC]), TUNDRA_VERSION, byte2, cache_lifetime, msg_id)
    return header + _tundra_ip_to_16_bytes(src_ip) + _tundra_ip_to_16_bytes(dst_ip)


def _tundra_decode_message(data, is_response=False):
    """Parse a 40-byte Tundra wireformat message into a dict.

    For requests, 4TO6 messages carry IPv4 addresses and 6TO4 messages carry
    IPv6 addresses. For responses the address family is flipped.
    """
    if len(data) != TUNDRA_MSG_SIZE:
        raise ValueError('invalid Tundra message size')
    magic, version, byte2, cache_lifetime, msg_id = struct.unpack('!cBBBI', data[0:8])
    if magic != bytes([TUNDRA_MAGIC]) or version != TUNDRA_VERSION:
        raise ValueError('invalid Tundra magic byte or protocol version')
    response = bool(byte2 & TUNDRA_BIT_RESPONSE)
    error = bool(byte2 & TUNDRA_BIT_ERROR)
    icmp = bool(byte2 & TUNDRA_BIT_ICMP)
    msg_type = byte2 & 0x1f
    # 4TO6 requests carry IPv4, 4TO6 responses carry IPv6 (and vice versa).
    expect_ipv4 = (msg_type in TUNDRA_4TO6_TYPES) if not is_response else (msg_type not in TUNDRA_4TO6_TYPES)
    src_ip = _tundra_16_bytes_to_ip(data[8:24], expect_ipv4)
    dst_ip = _tundra_16_bytes_to_ip(data[24:40], expect_ipv4)
    return {
        'response': response, 'error': error, 'icmp': icmp,
        'msg_type': msg_type, 'cache_lifetime': cache_lifetime,
        'msg_id': msg_id, 'src_ip': src_ip, 'dst_ip': dst_ip,
    }


def _client_ipv6(client_ipv4):
    """Render <client_ipv4> as fd64:ff9b::<client_ipv4_hex>."""
    address = int(IPv4Address(client_ipv4))
    high = (address >> 16) & 0xffff
    low = address & 0xffff
    return '{}{:x}:{:x}'.format(CLIENT_IPV6_PREFIX, high, low)


def _client_ipv4_from_ipv6(ipv6_address):
    """Extract the client IPv4 embedded in an fd64:ff9b::/96 address (last 4 bytes)."""
    packed = IPv6Address(ipv6_address).packed
    return str(IPv4Address(packed[12:16]))


class TundraExternalTranslationServer:
    """Unix SOCK_STREAM server implementing Tundra-NAT64's external address
    translation protocol.

    Tundra-NAT64 (addressing.mode = external, transport = unix) connects to
    this server and sends 40-byte wireformat requests; the server replies with
    40-byte responses built from dnsmap's SIIT binding tables
    (virtual IPv4 <-> real IPv6) and the deterministic
    client IPv4 <-> fd64:ff9b::<client_ipv4_hex> mapping.

    The server runs in a background thread and accepts one connection per
    Tundra translator thread. Each connection is handled in its own thread,
    reading 40-byte requests in a loop and replying synchronously.
    """

    def __init__(self, socket_path, resolver):
        self.socket_path = socket_path
        self.resolver = resolver
        self.server_socket = None
        self.available = False
        self._accept_thread = None
        self._stop_event = threading.Event()

    def start(self):
        try:
            parent = os.path.dirname(self.socket_path)
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
            self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server_socket.bind(self.socket_path)
            os.chmod(self.socket_path, 0o666)
            self.server_socket.listen(8)
            self.server_socket.settimeout(1.0)
            self.available = True
            self._stop_event.clear()
            self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
            self._accept_thread.start()
            print('Tundra: external address translation server listening on {}'.format(
                self.socket_path
            ))
        except OSError as error:
            self.available = False
            print('Tundra: external address translation server unavailable ({}): '
                  'IPv6 egress via Tundra disabled'.format(error))

    def stop(self):
        self.available = False
        self._stop_event.set()
        if self.server_socket is not None:
            try:
                self.server_socket.close()
            except OSError:
                pass
            self.server_socket = None

    def _accept_loop(self):
        while self.available and not self._stop_event.is_set():
            try:
                conn, _ = self.server_socket.accept()
            except (OSError, socket.timeout):
                continue
            threading.Thread(target=self._handle_connection, args=(conn,), daemon=True).start()

    def _handle_connection(self, conn):
        conn.settimeout(5)
        try:
            while self.available and not self._stop_event.is_set():
                data = self._read_exact(conn, TUNDRA_MSG_SIZE)
                if data is None:
                    return
                response = self._process_request(data)
                try:
                    conn.sendall(response)
                except OSError:
                    return
        except (OSError, socket.timeout):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _read_exact(self, conn, n):
        data = b''
        while len(data) < n:
            try:
                chunk = conn.recv(n - len(data))
            except socket.timeout:
                # Idle timeout: keep waiting if the server is still available.
                if not (self.available and not self._stop_event.is_set()):
                    return None
                continue
            if not chunk:
                return None
            data += chunk
        return data

    def _process_request(self, request):
        """Handle one 40-byte Tundra request and return a 40-byte response."""
        try:
            parsed = _tundra_decode_message(request)
        except ValueError:
            return _tundra_encode_message(True, True, False, 0, 0, 0, None, None)

        # Requests must not have the response or error bit set.
        if parsed['response'] or parsed['error']:
            return _tundra_encode_message(True, True, False, 0, 0, 0, None, None)

        msg_type = parsed['msg_type']
        msg_id = parsed['msg_id']
        src_ip = parsed['src_ip']
        dst_ip = parsed['dst_ip']

        try:
            result_src, result_dst, error, icmp = self._translate(msg_type, src_ip, dst_ip)
        except Exception as error_obj:
            print('Tundra: translation error for type {}: {}'.format(msg_type, error_obj))
            return _tundra_encode_message(True, True, False, msg_type, 0, msg_id, None, None)

        cache_lifetime = TUNDRA_CACHE_LIFETIME if not error else 0
        # Fallback responses (tundra_router_ipv4 substitution) must not be
        # cached by Tundra, so force cache_lifetime = 0 for those.
        if result_src == self.resolver.tundra_router_ipv4:
            cache_lifetime = 0
        return _tundra_encode_message(True, error, icmp, msg_type, cache_lifetime, msg_id, result_src, result_dst)

    def _translate(self, msg_type, src_ip, dst_ip):
        """Translate one address pair and return (src, dst, error, icmp).

        Tundra-NAT64 sends requests with icmp bit = 0. Packet type (MAIN vs ICMP_ERROR)
        is encoded in msg_type (bits 4-0). The ICMP flag (bit 5) only applies to
        error responses (error=1) on MAIN packet types to request an ICMP error
        back to the sender; it must be 0 on successful responses and on
        *_ICMP_ERROR message types.

        In ICMP error messages (4TO6_ICMP_ERROR, 6TO4_ICMP_ERROR), the
        inner packet's direction is reversed compared to the outer header:
        - 4TO6_ICMP_ERROR: inner src = VIP, inner dst = client_ipv4
        - 6TO4_ICMP_ERROR: inner src = client_ipv6, inner dst = real_ipv6
        """
        with self.resolver.siit_lock:
            # Point reads under lock - no dict copying needed.
            if msg_type == TUNDRA_MSG_TYPE_4TO6_MAIN:
                # IPv4 -> IPv6: source client IPv4 -> fd64:ff9b::<hex>,
                # destination VIP -> real IPv6 (SIIT binding).
                src_ipv6 = _client_ipv6(str(src_ip))
                dst_ipv6 = self.resolver.siit_by_vip.get(str(dst_ip))
                if dst_ipv6 is None:
                    return (None, None, True, False)
                self.resolver.siit_last_used[str(dst_ipv6)] = time.monotonic()
                return (src_ipv6, dst_ipv6, False, False)

            if msg_type == TUNDRA_MSG_TYPE_4TO6_ICMP_ERROR:
                # ICMP error: inner packet direction is reversed.
                # src_ip = VIP -> real IPv6 (SIIT binding),
                # dst_ip = client_ipv4 -> fd64:ff9b::<hex>.
                src_ipv6 = self.resolver.siit_by_vip.get(str(src_ip))
                if src_ipv6 is None:
                    return (None, None, True, False)
                self.resolver.siit_last_used[str(src_ipv6)] = time.monotonic()
                dst_ipv6 = _client_ipv6(str(dst_ip))
                return (src_ipv6, dst_ipv6, False, False)

            if msg_type == TUNDRA_MSG_TYPE_6TO4_MAIN:
                # IPv6 -> IPv4: source real IPv6 -> VIP (SIIT binding),
                # destination fd64:ff9b::<hex> -> client IPv4.
                # Validate that the destination is a client address.
                try:
                    dst_ipv6_addr = IPv6Address(dst_ip)
                except ValueError:
                    return (None, None, True, False)
                if dst_ipv6_addr not in CLIENT_IPV6_NETWORK:
                    return (None, None, True, False)
                # Normalize source IPv6 to canonical form (RFC 5952).
                src_ipv6_canonical = str(IPv6Address(src_ip))
                dst_ipv4 = _client_ipv4_from_ipv6(dst_ip)
                src_ipv4 = self.resolver.siit_by_real.get(src_ipv6_canonical)
                if src_ipv4 is None:
                    # Unknown source IPv6 but destination is a client address.
                    # Fall back to Tundra router address so the client gets
                    # ICMPv4 Fragmentation Needed instead of a black hole.
                    # The Tundra router address is routed via the TUN
                    # interface so incoming ICMPv4 packets are accepted.
                    # Do not cache this fallback (cache_lifetime = 0).
                    return (self.resolver.tundra_router_ipv4, dst_ipv4, False, False)
                self.resolver.siit_last_used[src_ipv6_canonical] = time.monotonic()
                return (src_ipv4, dst_ipv4, False, False)

            if msg_type == TUNDRA_MSG_TYPE_6TO4_ICMP_ERROR:
                # ICMP error: inner packet direction is reversed.
                # src_ip = client_ipv6 (must be in CLIENT_IPV6_NETWORK)
                #     -> _client_ipv4_from_ipv6(src),
                # dst_ip = real_ipv6 -> siit_by_real.get(dst).
                try:
                    src_ipv6_addr = IPv6Address(src_ip)
                except ValueError:
                    return (None, None, True, False)
                if src_ipv6_addr not in CLIENT_IPV6_NETWORK:
                    return (None, None, True, False)
                dst_ipv4 = self.resolver.siit_by_real.get(str(IPv6Address(dst_ip)))
                src_ipv4 = _client_ipv4_from_ipv6(src_ip)
                if dst_ipv4 is None:
                    return (None, None, True, False)
                self.resolver.siit_last_used[str(IPv6Address(dst_ip))] = time.monotonic()
                return (src_ipv4, dst_ipv4, False, False)

        return (None, None, True, False)


class DoHError(Exception):
    pass


class DoHResponseError(DoHError):
    pass


class DoHProtocolError(DoHError):
    pass


class ProxyResolver(BaseResolver):
    MAX_DOH_RESPONSE_SIZE = 65535
    ERROR_LOG_INTERVAL = 5
    MAPPING_BATCH_DELAY = 0.05
    """
        Proxy resolver - passes all requests to upstream DNS server and
        returns response

        Note that the request/response will be each be decoded/re-encoded
        twice:

        a) Request packet received by DNSHandler and parsed into DNSRecord
        b) DNSRecord passed to ProxyResolver, serialised back into packet
           and sent to upstream DNS server
        c) Upstream DNS server returns response packet which is parsed into
           DNSRecord
        d) ProxyResolver returns DNSRecord to DNSHandler which re-serialises
           this into packet and returns to client

        In practice this is actually fairly useful for testing but for a
        'real' transparent proxy option the DNSHandler logic needs to be
        modified (see PassthroughDNSHandler)

    """

    def __init__(self, address, port, doh_port, timeout, iprange, client_id,
                 resolver_client_id, asn_file, asn_database, tablename='dnsmap',
                 asn_database_reader=None, iptables_runner=None,
                 connection_factory=None, load_existing_mappings=True,
                 mapping_batch_delay=MAPPING_BATCH_DELAY,
                 tundra_socket=None, tundra_interface='tundra',
                 ipv6_client_id=None, excluded_addresses=None,
                 tundra_router_ipv4=TUNDRA_ROUTER_IPV4,
                 vip_stale_ttl=7200,
                 vip_cooldown_ttl=300,
                 conntrack_path='/proc/net/nf_conntrack',
                 aaaa_priority=False,
                 ipv6_egress_map=None):
        self.address = address
        self.port = port
        self.timeout = timeout
        self.doh_host = address
        self.doh_port = doh_port
        self.doh_connections = queue.LifoQueue(maxsize=32)
        self.connection_factory = connection_factory or http.client.HTTPConnection
        self.iptables_runner = iptables_runner or subprocess.run
        self.error_log_lock = threading.Lock()
        self.last_error_log = 0
        self.suppressed_errors = 0
        self.client_id = client_id
        self.resolver_client_id = resolver_client_id
        self.asn_file = asn_file
        self.asn_match_rules = AsnMatchRules({}, [], [])
        self.asn_list_lock = threading.RLock()
        asn_count, organization_count = self.reload_asn_list()
        print('Loaded {} ASN numbers and {} organization names'.format(
            asn_count, organization_count
        ))
        self.asn_database = asn_database_reader or maxminddb.open_database(asn_database)
        self.iprange = IPv4Network(iprange)
        self.excluded_addresses = set(excluded_addresses or ())
        self.unassigned_addresses = self.new_unassigned_addresses(
            self.excluded_addresses
        )
        self.mapping_capacity = len(self.unassigned_addresses)

        self.ipmap = {}
        self.ipmap_last_used = {}  # real_ipv4 -> time.monotonic()
        self.tablename = tablename
        self.mapping_lock = threading.RLock()
        self.mapping_condition = threading.Condition(self.mapping_lock)
        self.pending_mappings = {}
        self.mapping_batch = []
        self.mapping_batch_delay = mapping_batch_delay
        self.mapping_batch_running = False
        self.mapping_stopping = False

        # Tundra-NAT64 (packet SIIT) integration.
        self.tundra_socket = tundra_socket
        self.tundra_interface = tundra_interface
        self.tundra_router_ipv4 = tundra_router_ipv4
        self.vip_stale_ttl = vip_stale_ttl
        self.vip_cooldown_ttl = vip_cooldown_ttl
        self.aaaa_priority = aaaa_priority
        self.conntrack_path = conntrack_path
        # Per-domain IPv6 egress group: domain -> 'local' | 'world' | 'direct'.
        # 'local' uses az-ipv6-* client ID of the local exit node,
        # 'world' uses az-ipv6-* client ID of the world exit node,
        # 'direct' skips IPv6 egress (IPv4 only).
        self.ipv6_egress_map = {
            k.rstrip('.').lower(): v for k, v in (ipv6_egress_map or {}).items()
        }
        # Quarantined VIPs: (release_at, vip) tuples under mapping_lock.
        # Evicted VIPs are held here for vip_cooldown_ttl seconds before being
        # returned to unassigned_addresses, preventing immediate reissue.
        self.quarantined_vips = deque()
        if ipv6_client_id is None and tundra_socket and isinstance(client_id, str) and client_id.startswith('az-'):
            ipv6_client_id = client_id.replace('az-', 'az-ipv6-', 1)
        self.ipv6_client_id = ipv6_client_id
        # SIIT bindings: virtual IPv4 <-> real IPv6. Shared address pool with
        # regular IPv4 mappings; no separate reserve. These tables are the
        # source of truth consulted by the Tundra external translation server.
        self.siit_by_vip = {}
        self.siit_by_real = {}
        self.siit_last_used = {}  # target_ipv6 -> time.monotonic()
        self.siit_lock = threading.RLock()
        # pending_siit: target_ipv6 -> threading.Event, used to serialize
        # concurrent SIIT VIP allocation for the same IPv6 destination so that
        # parallel DNS queries for the same AAAA record get the same VIP and
        # only one binding is created.
        self.pending_siit = {}
        # External address translation server (dnsmap is the server, Tundra
        # connects to it as the client).
        self.tundra_server = TundraExternalTranslationServer(tundra_socket, self) if tundra_socket else None

        # Load existing mappings from the dnsmap iptables chain, including
        # SIIT bindings persisted as comment rules, BEFORE starting the
        # translation server so that Tundra connects to a fully initialized
        # resolver.
        if load_existing_mappings:
            self._load_existing_mappings()

        if self.tundra_server is not None:
            self.tundra_server.start()

    # ------------------------------------------------------------------
    # ASN list (unchanged from dev core)
    # ------------------------------------------------------------------

    @staticmethod
    def organization_substring_rule(line):
        return (line, line.casefold())

    @staticmethod
    def organization_regex_rule(line):
        if not (line.startswith('/') and line.endswith('/')):
            raise ValueError('Organization regex must use /pattern/ format')
        pattern = line[1:-1]
        if not pattern:
            raise ValueError('Empty organization regex')
        return (line, re.compile(pattern, re.IGNORECASE | re.UNICODE))

    @staticmethod
    def domain_matcher(condition):
        kind, _, pattern = condition.partition(':')
        if kind == 'domain':
            domain = idna.encode(pattern, uts46=True).decode('ascii').rstrip('.')
            pattern = r'(?:^|\.)' + re.escape(domain) + r'$'
        elif kind != 'regexp' or not pattern:
            raise ValueError('Invalid ASN domain condition: {}'.format(condition))
        return re.compile(pattern, re.IGNORECASE)

    @classmethod
    def load_asn_list(cls, path):
        asn_rules = {}
        substring_rules = []
        regex_rules = []
        domain_rules = []
        paths = [item.strip() for item in str(path).split(';') if item.strip()]
        for asn_path in paths:
            with open(asn_path, encoding='utf-8') as asn_list:
                for raw_line in asn_list:
                    line = raw_line.split('#', 1)[0].strip()
                    if not line:
                        continue
                    parts = line.split(None, 1)
                    asn_match = re.fullmatch(r'(?:AS)?([0-9]+)', parts[0], re.IGNORECASE)
                    if asn_match:
                        asn = int(asn_match[1])
                        if len(parts) == 1:
                            asn_rules[asn] = line
                        else:
                            domain_rules.append((asn, line, cls.domain_matcher(parts[1])))
                    elif line.startswith('/') and line.endswith('/'):
                        regex_rules.append(cls.organization_regex_rule(line))
                    else:
                        substring_rules.append(cls.organization_substring_rule(line))
        return AsnMatchRules(asn_rules, substring_rules, regex_rules, domain_rules)

    def reload_asn_list(self):
        with self.asn_list_lock:
            rules = self.load_asn_list(self.asn_file)
            self.asn_match_rules = rules
            return (len(rules.asn_rules) + len(rules.domain_rules),
                    len(rules.substring_rules) + len(rules.regex_rules))

    def request_asn_reload(self, signum, frame):
        try:
            asn_count, organization_count = self.reload_asn_list()
            os.write(1, 'Loaded {} ASN numbers and {} organization names\n'.format(
                asn_count, organization_count
            ).encode())
        except Exception as error:
            os.write(2, 'Failed to reload ASN list, keeping previous list: {}\n'.format(
                error
            ).encode())

    # ------------------------------------------------------------------
    # AdGuard az-ipv6-* authorization for IPv6 egress
    # ------------------------------------------------------------------

    def check_ipv6_authorized(self, domain):
        """Ask AdGuard (az-ipv6-* ClientID) whether a domain may use IPv6 egress.

        Per-domain egress group (from self.ipv6_egress_map) selects the
        AdGuard client ID:
          - 'local'  -> az-ipv6-<local client id suffix>
          - 'world'  -> az-ipv6-<world client id suffix>
          - 'direct' -> skip IPv6 egress (return DENIED)
        Domains not in the map use the default self.ipv6_client_id.

        Returns the constant IPV6_EGRESS_AUTHORIZED if AdGuard returned
        NOERROR with A records, IPV6_EGRESS_DENIED if AdGuard returned
        SERVFAIL or REFUSED (explicitly denied), or IPV6_EGRESS_ERROR if
        the query itself failed (technical error, caller decides fallback).
        """
        # Normalize domain: strip trailing dot and lowercase for consistent
        # map lookup (DNS is case-insensitive).
        domain = domain.rstrip('.').lower()
        # Determine the client ID for this domain based on its egress group.
        egress_group = self.ipv6_egress_map.get(domain)
        if egress_group == 'direct':
            print('IPv6 egress: {} marked direct, skipping IPv6 egress'.format(domain))
            return IPV6_EGRESS_DENIED
        client_id = self.ipv6_client_id
        if egress_group == 'local':
            client_id = 'az-ipv6-local'
        elif egress_group == 'world':
            client_id = 'az-ipv6-world'
        if not client_id:
            return IPV6_EGRESS_ERROR
        try:
            reply = self.query_doh(DNSRecord.question(domain, 'A'), client_id)
            if reply.header.rcode == RCODE.NOERROR:
                authorized = any(record.rtype == QTYPE.A for record in reply.rr)
                if authorized:
                    print('IPv6 egress: AdGuard authorized {} via ClientID {}'.format(
                        domain, client_id
                    ))
                    return IPV6_EGRESS_AUTHORIZED
                print('IPv6 egress: AdGuard NOERROR without A for {} via ClientID {}'.format(
                    domain, client_id
                ))
                return IPV6_EGRESS_AUTHORIZED
            elif reply.header.rcode in (RCODE.SERVFAIL, RCODE.REFUSED):
                # AdGuard denies IPv6 egress with either SERVFAIL or REFUSED
                # catch-all dnsrewrite rules (dnsrewrite=SERVFAIL or
                # dnsrewrite=REFUSED). Both mean the domain is explicitly not
                # authorized for IPv6 egress.
                print('IPv6 egress: AdGuard rcode={} for {} via ClientID {} - denied'.format(
                    reply.header.rcode, domain, client_id
                ))
                return IPV6_EGRESS_DENIED
            else:
                print('IPv6 egress: AdGuard rcode={} for {} via ClientID {} - error'.format(
                    reply.header.rcode, domain, client_id
                ))
                return IPV6_EGRESS_ERROR
        except Exception as error:
            print('IPv6 egress: AdGuard query failed for {}: {} - error'.format(domain, error))
            return IPV6_EGRESS_ERROR

    # ------------------------------------------------------------------
    # Tundra-NAT64 SIIT binding helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _client_address(handler):
        """Best-effort client IPv4 address from the DNS handler."""
        try:
            address = handler.client_address
        except Exception:
            return '0.0.0.0'
        if isinstance(address, (tuple, list)) and address:
            return address[0]
        return str(address)

    def _client_ipv6(self, client_ipv4):
        """Render <client_ipv4> as fd64:ff9b::<client_ipv4_hex>."""
        return _client_ipv6(client_ipv4)

    def _allocate_siit_vip(self):
        """Allocate a virtual IPv4 from the shared pool for a SIIT binding.

        Returns the VIP or None if the pool is exhausted. On exhaustion the
        pool is deliberately NOT flushed: existing bindings that may still be
        present in client DNS caches are preserved, unless stale VIPs can be
        evicted via _evict_stale_vips.
        """
        with self.mapping_condition:
            self._reclaim_cooled_down_vips()
            if self.mapping_stopping:
                return None
            if self.unassigned_addresses:
                return self.unassigned_addresses.popleft()
        self._evict_stale_vips(needed=1)
        with self.mapping_condition:
            self._reclaim_cooled_down_vips()
            if self.mapping_stopping or not self.unassigned_addresses:
                return None
            return self.unassigned_addresses.popleft()

    def _rollback_siit_iptables(self, vip, ipv6):
        """Remove the SIIT comment rule for a VIP.

        Returns True if the rule was removed successfully, False otherwise.
        """
        del_result = self.iptables_runner(
            ['iptables', '-w', '-t', 'nat', '-D', self.tablename,
             '-d', vip, '-m', 'comment', '--comment',
             '{}{}'.format(SIIT_COMMENT_PREFIX, ipv6), '-j', 'RETURN'],
            capture_output=True, text=True,
        )
        if del_result is None or del_result.returncode != 0:
            print('ERROR: Failed to rollback SIIT iptables rule for {}: {}'.format(
                vip, getattr(del_result, 'stderr', '') or 'no result'
            ))
            return False
        return True

    def _setup_siit_binding(self, vip, ipv6, restore=False):
        """Install a SIIT binding: iptables comment rule + /32 route.

        The binding and route are established strictly before the DNS answer
        that uses <vip> is sent, so clients never receive a blackholed VIP.
        The actual SIIT translation table lives in this resolver's
        siit_by_vip / siit_by_real dictionaries, which the Tundra external
        translation server consults per-packet; there is no separate ADD
        command to push bindings to Tundra.
        When restoring after a restart the iptables rule already exists
        (persisted by the save/restore mechanism), so only the route is
        recreated.
        Returns (ok, can_return_vip_to_pool):
          ok=True: binding installed successfully.
          ok=False, can_return_vip_to_pool=True: route failed, rollback succeeded,
              VIP can be returned to the unassigned pool.
          ok=False, can_return_vip_to_pool=False: route failed AND rollback failed,
              VIP must NOT be returned to the pool (it remains excluded).
        """
        # 1. Persist the binding in the dnsmap chain as a comment rule.
        if not restore:
            rules = ['*nat',
                     '-A {} -d {} -m comment --comment "{}{}" -j RETURN'.format(
                         self.tablename, vip, SIIT_COMMENT_PREFIX, ipv6),
                     'COMMIT', '']
            try:
                result = self.iptables_runner(
                    ['iptables-restore', '--wait', '--noflush'],
                    input='\n'.join(rules), capture_output=True, text=True,
                )
                if result is None or result.returncode != 0:
                    print('ERROR: Failed to install SIIT iptables rule for {}: {}'.format(
                        vip, getattr(result, 'stderr', '') or 'no result'
                    ))
                    return False, False
            except OSError as error:
                print('ERROR: Failed to install SIIT iptables rule for {}: {}'.format(vip, error))
                return False, False

        # 2. Route the VIP /32 to the Tundra TUN interface.
        try:
            result = subprocess.run(
                ['ip', 'route', 'replace', '{}/32'.format(vip), 'dev', self.tundra_interface],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print('ERROR: Failed to route {}/32 to {}: {}'.format(
                    vip, self.tundra_interface, result.stderr.strip()
                ))
                if not self._rollback_siit_iptables(vip, ipv6):
                    return False, False  # Rollback failed: VIP stays excluded
                return False, True  # Rollback succeeded: VIP can return to pool
        except OSError as error:
            print('ERROR: Failed to route {}/32 to {}: {}'.format(
                vip, self.tundra_interface, error
            ))
            if not self._rollback_siit_iptables(vip, ipv6):
                return False, False  # Rollback failed: VIP stays excluded
            return False, True  # Rollback succeeded: VIP can return to pool

        # 3. Record the binding in the in-memory SIIT tables consulted by the
        #    Tundra external translation server.
        with self.siit_lock:
            self.siit_by_vip[vip] = ipv6
            self.siit_by_real[ipv6] = vip
            self.siit_last_used[ipv6] = time.monotonic()
        print('SIIT binding {} -> {} installed (route via {})'.format(
            vip, ipv6, self.tundra_interface
        ))
        return True, False

    def _vip_has_active_conntrack(self, vip):
        """Check /proc/net/nf_conntrack for active connections involving <vip>.

        Returns True if the VIP appears as src or dst in an active conntrack
        entry, False if no active connection is found, or None if the conntrack
        file is unavailable (OSError / FileNotFoundError).
        """
        try:
            with open(self.conntrack_path, 'r') as f:
                for line in f:
                    # conntrack lines look like:
                    # ipv4     2 tcp      6 431989 ESTABLISHED src=1.2.3.4 dst=5.6.7.8 sport=1234 dport=5678 ...
                    if 'dst={} '.format(vip) in line or 'src={} '.format(vip) in line:
                        return True
            return False
        except (OSError, FileNotFoundError):
            # /proc/net/nf_conntrack not available (no CAP_NET_ADMIN, etc.)
            print('WARNING: cannot read {} - cannot check conntrack for VIP {}'.format(
                self.conntrack_path, vip))
            return None

    def _reclaim_cooled_down_vips(self):
        """Move cooled-down VIPs from quarantined_vips back to unassigned_addresses.

        Called under mapping_condition at the start of VIP allocation so that
        evicted VIPs get a cooldown period (vip_cooldown_ttl) before being
        reissued, preventing immediate reuse after eviction.

        Before returning a VIP to the pool, re-checks conntrack: if the VIP
        still has active connections (or conntrack is unavailable), it stays
        quarantined with an extended TTL.
        """
        now = time.monotonic()
        released = []
        with self.mapping_lock:
            remaining = deque()
            while self.quarantined_vips:
                release_at, vip = self.quarantined_vips.popleft()
                if release_at > now:
                    remaining.append((release_at, vip))
                    continue
                # Re-check conntrack before releasing: if the VIP still has
                # active connections (or conntrack is unavailable), keep it
                # quarantined with an extended TTL.
                ct = self._vip_has_active_conntrack(vip)
                if ct is True:
                    remaining.append((now + max(self.vip_cooldown_ttl, 60), vip))
                    continue
                if ct is None:
                    remaining.append((now + max(self.vip_cooldown_ttl, 60), vip))
                    continue
                released.append(vip)
            self.quarantined_vips = remaining
        if released:
            with self.mapping_condition:
                for vip in released:
                    try:
                        self.unassigned_addresses.append(vip)
                    except ValueError:
                        pass
                    print('IPv6 via Tundra: VIP {} returned from cooldown to pool'.format(vip))

    def _evict_stale_vips(self, needed=1):
        """Evict stale VIPs to free up pool addresses.

        Called only when unassigned_addresses is exhausted. Selects the oldest
        entries (regular IPv4 DNAT and SIIT bindings) whose last_used timestamp
        is older than vip_stale_ttl, removes their iptables rules/routes, and
        returns the VIP to the unassigned pool only if iptables -D succeeded.

        To avoid a TOCTOU race where a parallel DNS query could still use a
        VIP that is being evicted, the in-memory mapping is removed under lock
        *before* calling iptables -D. If iptables -D fails, the mapping is
        restored under lock. Only after a successful iptables -D is the VIP
        returned to the unassigned pool.

        Returns the number of VIPs freed.
        """
        now = time.monotonic()
        cutoff = now - self.vip_stale_ttl

        # Collect stale candidates under locks (quick reads only).
        candidates = []
        with self.mapping_lock:
            for real_addr, last_used in list(self.ipmap_last_used.items()):
                if last_used <= cutoff:
                    fake_addr = self.ipmap.get(real_addr)
                    if fake_addr is not None:
                        candidates.append((last_used, 'ipv4', real_addr, fake_addr))

        with self.siit_lock:
            for target_ipv6, last_used in list(self.siit_last_used.items()):
                if last_used <= cutoff:
                    vip = self.siit_by_real.get(target_ipv6)
                    if vip is not None:
                        candidates.append((last_used, 'siit', target_ipv6, vip))

        # Sort by age (oldest first).
        candidates.sort(key=lambda x: x[0])

        freed = 0
        for last_used, entry_type, key, vip in candidates:
            if freed >= needed:
                break

            # Skip VIPs with active conntrack entries - they are still in use.
            ct = self._vip_has_active_conntrack(vip)
            if ct is True:
                with self.mapping_lock:
                    if entry_type == 'ipv4':
                        self.ipmap_last_used[key] = time.monotonic()
                    else:
                        self.siit_last_used[key] = time.monotonic()
                continue
            # If conntrack is unavailable (None), fail-closed: do not evict.
            if ct is None:
                continue

            if entry_type == 'ipv4':
                real_addr = key
                # Re-check last_used under lock, then atomically remove the
                # mapping from memory so parallel DNS queries cannot use it
                # while iptables -D is in flight.
                with self.mapping_lock:
                    current_used = self.ipmap_last_used.get(real_addr)
                    if current_used is not None and current_used > cutoff:
                        continue
                    current_vip = self.ipmap.get(real_addr)
                    if current_vip is None or current_vip != vip:
                        continue
                    if real_addr in self.pending_mappings:
                        continue
                    # Remove from memory immediately; VIP will be returned to
                    # the pool only after iptables -D succeeds (or to
                    # quarantine if vip_cooldown_ttl > 0).
                    self.ipmap.pop(real_addr, None)
                    self.ipmap_last_used.pop(real_addr, None)

                # Remove iptables DNAT rule OUTSIDE the lock.
                del_result = self.iptables_runner(
                    ['iptables', '-w', '-t', 'nat', '-D', self.tablename,
                     '-d', vip, '-j', 'DNAT', '--to-destination', real_addr],
                    capture_output=True, text=True,
                )
                if del_result is None or del_result.returncode != 0:
                    print('ERROR: Failed to remove DNAT rule for {}: {}'.format(
                        vip, getattr(del_result, 'stderr', '') or 'no result'
                    ))
                    # Restore the mapping so it stays usable.
                    with self.mapping_lock:
                        if real_addr not in self.ipmap:
                            self.ipmap[real_addr] = vip
                            self.ipmap_last_used[real_addr] = current_used
                    continue

                # iptables -D succeeded: return VIP to pool or quarantine.
                with self.mapping_lock:
                    if self.vip_cooldown_ttl > 0:
                        self.quarantined_vips.append(
                            (time.monotonic() + self.vip_cooldown_ttl, vip)
                        )
                    else:
                        try:
                            self.unassigned_addresses.append(vip)
                        except ValueError:
                            pass
                print('Evicted stale IPv4 mapping {} -> {} (last used {:.0f}s ago)'.format(
                    vip, real_addr, now - last_used
                ))
                freed += 1

            elif entry_type == 'siit':
                target_ipv6 = key
                # Re-check last_used under lock, then atomically remove the
                # binding from memory so parallel DNS queries cannot use it
                # while iptables -D is in flight.
                with self.siit_lock:
                    current_used = self.siit_last_used.get(target_ipv6)
                    if current_used is not None and current_used > cutoff:
                        continue
                    current_vip = self.siit_by_real.get(target_ipv6)
                    if current_vip is None or current_vip != vip:
                        continue
                    if target_ipv6 in self.pending_siit:
                        continue
                    # Remove from memory immediately; VIP will be returned to
                    # the pool only after iptables -D and ip route del succeed.
                    self.siit_by_vip.pop(vip, None)
                    self.siit_by_real.pop(target_ipv6, None)
                    self.siit_last_used.pop(target_ipv6, None)

                # Remove iptables comment rule first OUTSIDE the lock.
                del_result = self.iptables_runner(
                    ['iptables', '-w', '-t', 'nat', '-D', self.tablename,
                     '-d', vip, '-m', 'comment', '--comment',
                     '{}{}'.format(SIIT_COMMENT_PREFIX, target_ipv6), '-j', 'RETURN'],
                    capture_output=True, text=True,
                )
                if del_result is None or del_result.returncode != 0:
                    print('ERROR: Failed to remove SIIT iptables rule for {}: {}'.format(
                        vip, getattr(del_result, 'stderr', '') or 'no result'
                    ))
                    # Restore the binding so it stays usable.
                    with self.siit_lock:
                        if target_ipv6 not in self.siit_by_real and vip not in self.siit_by_vip:
                            self.siit_by_vip[vip] = target_ipv6
                            self.siit_by_real[target_ipv6] = vip
                            self.siit_last_used[target_ipv6] = current_used
                    continue

                # Remove route OUTSIDE the lock.
                route_result = subprocess.run(
                    ['ip', 'route', 'del', '{}/32'.format(vip), 'dev', self.tundra_interface],
                    capture_output=True, text=True,
                )
                if route_result.returncode != 0:
                    stderr = route_result.stderr.strip()
                    # "No such process" (ESRCH) means the route is already gone,
                    # which is acceptable.
                    if 'No such process' not in stderr and 'No such file' not in stderr:
                        print('ERROR: Failed to delete route {}/32: {}'.format(
                            vip, stderr
                        ))
                        # Try to restore the iptables rule we just removed so
                        # that iptables, routes and memory stay consistent.
                        restore_rules = '*nat\n-A {} -d {} -m comment --comment "{}{}" -j RETURN\nCOMMIT\n'.format(
                            self.tablename, vip, SIIT_COMMENT_PREFIX, target_ipv6
                        )
                        restore_result = self.iptables_runner(
                            ['iptables-restore', '--wait', '--noflush'],
                            input=restore_rules, capture_output=True, text=True,
                        )
                        if restore_result is not None and restore_result.returncode == 0:
                            print('ERROR: Restored iptables rule for {} after route del failure'.format(vip))
                            with self.siit_lock:
                                if target_ipv6 not in self.siit_by_real and vip not in self.siit_by_vip:
                                    self.siit_by_vip[vip] = target_ipv6
                                    self.siit_by_real[target_ipv6] = vip
                                    self.siit_last_used[target_ipv6] = current_used
                            continue
                        # Both route del and iptables-restore failed: quarantine
                        # the binding (remove from memory, do NOT return VIP to pool).
                        print('ERROR: iptables-restore failed for {}; quarantining binding'.format(vip))
                        continue

                # Both iptables -D and ip route del succeeded: return VIP to pool or quarantine.
                with self.mapping_lock:
                    if self.vip_cooldown_ttl > 0:
                        self.quarantined_vips.append(
                            (time.monotonic() + self.vip_cooldown_ttl, vip)
                        )
                    else:
                        try:
                            self.unassigned_addresses.append(vip)
                        except ValueError:
                            pass
                print('Evicted stale SIIT binding {} -> {} (last used {:.0f}s ago)'.format(
                    vip, target_ipv6, now - last_used
                ))
                freed += 1

        return freed

    # ------------------------------------------------------------------
    # Existing mapping loading (regular IPv4 DNAT + SIIT comments)
    # ------------------------------------------------------------------

    def _load_existing_mappings(self):
        """Load existing mappings from the dnsmap iptables chain.

        Regular DNAT mappings are restored via add_mapping. SIIT bindings are
        parsed from comment rules (dnsmap:siit:v1:<ipv6>), added to the SIIT
        tables, and their routes are recreated.

        Uses `iptables -S dnsmap` (not `-nL`) because `-nL` does not print
        `-d` / `--comment` flags — comments appear as `/* ... */` at end of
        line and cannot be parsed reliably.
        """
        try:
            output = subprocess.check_output(
                ['iptables', '-w', '-t', 'nat', '-S', self.tablename],
                encoding='utf-8', stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            print('Failed to load existing mappings: {}'.format(error))
            output = ''

        for line in output.splitlines():
            line = line.strip()
            if not line.startswith('-A {} '.format(self.tablename)):
                continue
            parts = line.split()

            # SIIT comment rule:
            #   -A dnsmap -d <vip>[/32] -m comment --comment "dnsmap:siit:v1:<ipv6>" -j RETURN
            if '--comment' in parts:
                vip = None
                ipv6 = None
                for index, part in enumerate(parts):
                    if part == '-d' and index + 1 < len(parts):
                        vip = parts[index + 1]
                        if vip.endswith('/32'):
                            vip = vip[:-3]
                    if part == '--comment' and index + 1 < len(parts):
                        comment = parts[index + 1].strip('"')
                        if comment.startswith(SIIT_COMMENT_PREFIX):
                            ipv6 = comment[len(SIIT_COMMENT_PREFIX):]
                if vip and ipv6:
                    try:
                        IPv4Address(vip)
                        ipv6 = str(IPv6Address(ipv6))
                    except ValueError:
                        continue
                    with self.mapping_lock:
                        try:
                            self.unassigned_addresses.remove(vip)
                        except ValueError:
                            pass  # already accounted for
                    ok, can_return = self._setup_siit_binding(vip, ipv6, restore=True)
                    if ok:
                        print('Restored SIIT binding {} -> {}'.format(vip, ipv6))
                    elif can_return:
                        # Route setup failed but rollback succeeded: return VIP to pool.
                        with self.mapping_lock:
                            try:
                                self.unassigned_addresses.appendleft(vip)
                            except ValueError:
                                pass
                    continue
                continue

            # Regular DNAT rule:
            #   -A dnsmap -d <vip>[/32] -j DNAT --to-destination <real_ipv4>
            if '-j' in parts and 'DNAT' in parts:
                vip = None
                real_addr = None
                for index, part in enumerate(parts):
                    if part == '-d' and index + 1 < len(parts):
                        vip = parts[index + 1]
                        if vip.endswith('/32'):
                            vip = vip[:-3]
                    if part == '--to-destination' and index + 1 < len(parts):
                        real_addr = parts[index + 1]
                if vip and real_addr:
                    if not self.add_mapping(real_addr, vip):
                        print("ERROR: Failed to load mapping {} to {}, ignoring".format(
                            vip, real_addr
                        ))
                    else:
                        with self.mapping_lock:
                            self.ipmap_last_used[real_addr] = time.monotonic()

    # ------------------------------------------------------------------
    # DoH client (unchanged from dev core)
    # ------------------------------------------------------------------

    def query_doh(self, request, client_id):
        dns = base64.urlsafe_b64encode(request.pack()).rstrip(b'=').decode('ascii')
        path = '/dns-query/{}?dns={}'.format(quote(client_id, safe=''), dns)

        for attempt in range(2):
            if attempt == 0:
                try:
                    connection = self.doh_connections.get_nowait()
                except queue.Empty:
                    connection = self.connection_factory(
                        self.doh_host, self.doh_port, timeout=self.timeout
                    )
            else:
                connection = self.connection_factory(
                    self.doh_host, self.doh_port, timeout=self.timeout
                )

            reusable = False
            try:
                connection.request('GET', path, headers={'Accept': 'application/dns-message'})
                response = connection.getresponse()
                if response.status != 200:
                    raise DoHResponseError('DoH server returned HTTP {}'.format(response.status))
                content_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
                if content_type and content_type != 'application/dns-message':
                    raise DoHResponseError('Unexpected DoH content type {}'.format(content_type))
                content_length = response.getheader('Content-Length')
                if content_length:
                    try:
                        content_length = int(content_length)
                    except ValueError as error:
                        raise DoHResponseError('Invalid DoH Content-Length') from error
                    if content_length > self.MAX_DOH_RESPONSE_SIZE:
                        raise DoHResponseError('DoH response is too large')
                response_data = response.read(self.MAX_DOH_RESPONSE_SIZE + 1)
                if len(response_data) > self.MAX_DOH_RESPONSE_SIZE:
                    raise DoHResponseError('DoH response is too large')
                try:
                    parsed_response = DNSRecord.parse(response_data)
                except Exception as error:
                    raise DoHProtocolError('Invalid DNS response: {}'.format(error)) from error
                reusable = not response.will_close
                return parsed_response
            except (OSError, http.client.HTTPException):
                connection.close()
                if attempt == 1:
                    raise
            except Exception:
                connection.close()
                raise
            finally:
                if reusable:
                    try:
                        self.doh_connections.put_nowait(connection)
                    except queue.Full:
                        connection.close()

    def close(self):
        with self.mapping_condition:
            self.mapping_stopping = True
            self.mapping_condition.notify_all()
            while self.mapping_batch_running:
                self.mapping_condition.wait()
        while True:
            try:
                self.doh_connections.get_nowait().close()
            except queue.Empty:
                break
        close_database = getattr(self.asn_database, 'close', None)
        if close_database:
            close_database()
        if self.tundra_server is not None:
            self.tundra_server.stop()

    def log_processing_error(self, error):
        now = time.monotonic()
        with self.error_log_lock:
            if now - self.last_error_log < self.ERROR_LOG_INTERVAL:
                self.suppressed_errors += 1
                return
            suffix = ''
            if self.suppressed_errors:
                suffix = ' ({} similar errors suppressed)'.format(self.suppressed_errors)
            self.suppressed_errors = 0
            self.last_error_log = now
        print('DNS request processing failed: {}{}'.format(error, suffix))
        traceback.print_exc()

    def is_blocked_asn(self, address, hostname=''):
        if not address or address == '0.0.0.0':
            return False
        record = self.asn_database.get(address)
        print('ASN lookup for {}: {}'.format(address, record))
        if not record:
            print('ASN data not found for {}'.format(address))
            return False
        asn = record.get('autonomous_system_number')
        raw_organization = record.get('autonomous_system_organization') or ''
        rule = None
        rules = self.asn_match_rules
        if asn in rules.asn_rules:
            rule = rules.asn_rules[asn]
        else:
            folded_organization = raw_organization.casefold()
            for rule_line, matcher in rules.substring_rules:
                if matcher in folded_organization:
                    rule = rule_line
                    break
            if not rule:
                for rule_line, matcher in rules.regex_rules:
                    if matcher.search(raw_organization):
                        rule = rule_line
                        break
        if not rule and hostname:
            hostname = hostname.rstrip('.').lower()
            for rule_asn, rule_line, matcher in rules.domain_rules:
                if asn == rule_asn and matcher.search(hostname):
                    rule = rule_line
                    break
        if rule:
            print('ASN match for {}: rule: {}'.format(address, rule))
            return True
        print('ASN no match for {}: AS{}; organization: {}'.format(address, asn, raw_organization))
        return False

    def get_mapping(self, real_addr):
        with self.mapping_lock:
            return self.ipmap.get(real_addr)

    def get_siit_mapping(self, vip):
        with self.siit_lock:
            return self.siit_by_vip.get(vip)

    def get_siit_mapping_by_real(self, ipv6):
        with self.siit_lock:
            return self.siit_by_real.get(ipv6)

    def new_unassigned_addresses(self, excluded_addresses=None):
        addresses = deque([str(address) for address in self.iprange.hosts()])
        # Preserve the first address from the range for DNS.
        try:
            addresses.popleft()
        except IndexError as error:
            raise ValueError('IP range has no addresses available for mappings') from error
        # Exclude service addresses that must never be assigned as VIPs
        # (Tundra router addresses, DNS fallback address, etc.).
        if excluded_addresses:
            exclude_set = set(excluded_addresses)
            addresses = deque(
                addr for addr in addresses if addr not in exclude_set
            )
        if not addresses:
            raise ValueError('IP range has no addresses available for mappings')
        return addresses

    def add_mapping(self, real_addr, fake_addr=None):
        if fake_addr:
            with self.mapping_lock:
                existing_fake_addr = self.get_mapping(real_addr)
                if existing_fake_addr:
                    # Update last-used timestamp for reuse.
                    self.ipmap_last_used[real_addr] = time.monotonic()
                    print("ERROR: Real addr {} is already mapped to {}, ignoring duplicate mapping to {}".format(
                        real_addr, existing_fake_addr, fake_addr
                    ))
                    return True
                try:
                    self.unassigned_addresses.remove(fake_addr)
                    self.ipmap[real_addr] = fake_addr
                    self.ipmap_last_used[real_addr] = time.monotonic()
                    print('Mapping {} to {}'.format(fake_addr, real_addr))
                except ValueError:
                    print("ERROR: Fake addr {} not in unassigned addresses list".format(fake_addr))
                    return False
                return True

        return self.add_mappings([real_addr]).get(real_addr, False)

    def add_mappings(self, real_addresses):
        unique_addresses = list(dict.fromkeys(real_addresses))
        if len(unique_addresses) > self.mapping_capacity:
            print('ERROR: Not enough fake IP addresses for {} mappings'.format(
                len(unique_addresses)
            ))
            return {real_addr: None for real_addr in unique_addresses}

        with self.mapping_condition:
            self._reclaim_cooled_down_vips()
            if self.mapping_stopping:
                return {real_addr: None for real_addr in unique_addresses}

            new_addresses = [
                real_addr
                for real_addr in unique_addresses
                if not self.get_mapping(real_addr)
                and real_addr not in self.pending_mappings
            ]
            if len(new_addresses) > len(self.unassigned_addresses):
                # Pool exhausted: try to evict stale VIPs before giving up.
                # Release the condition lock while evicting (may sleep due to iptables).
                needed = len(new_addresses) - len(self.unassigned_addresses)
                self.mapping_condition.release()
                try:
                    self._evict_stale_vips(needed=needed)
                finally:
                    self.mapping_condition.acquire()
                if self.mapping_stopping:
                    return {real_addr: None for real_addr in unique_addresses}
                # Re-check capacity under lock after eviction.
                new_addresses = [
                    real_addr
                    for real_addr in unique_addresses
                    if not self.get_mapping(real_addr)
                    and real_addr not in self.pending_mappings
                ]
                if len(new_addresses) > len(self.unassigned_addresses):
                    # Still exhausted: do NOT flush the chain (that would
                    # destroy SIIT bindings) and do NOT reassign addresses
                    # that may still be present in client DNS caches.
                    return {real_addr: None for real_addr in unique_addresses}

            requests = {}
            is_batch_leader = False
            for real_addr in unique_addresses:
                existing_fake_addr = self.get_mapping(real_addr)
                if existing_fake_addr:
                    # Update last-used timestamp for existing mappings.
                    self.ipmap_last_used[real_addr] = time.monotonic()
                    continue
                existing_request = self.pending_mappings.get(real_addr)
                if existing_request:
                    requests[real_addr] = existing_request
                    continue

                fake_addr = self.unassigned_addresses.popleft()
                request = MappingRequest(real_addr, fake_addr, threading.Event())
                self.pending_mappings[real_addr] = request
                self.mapping_batch.append(request)
                requests[real_addr] = request
            if requests:
                if not self.mapping_batch_running:
                    self.mapping_batch_running = True
                    is_batch_leader = True
                self.mapping_condition.notify()

        if is_batch_leader:
            self.apply_mapping_batches()

        for request in requests.values():
            request.event.wait()

        return {
            real_addr: self.get_mapping(real_addr)
            for real_addr in unique_addresses
        }

    def apply_mapping_batches(self):
        while True:
            with self.mapping_condition:
                deadline = time.monotonic() + self.mapping_batch_delay
                while not self.mapping_stopping:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.mapping_condition.wait(remaining)

                batch = self.mapping_batch
                self.mapping_batch = []

            rules = ['*nat']
            rules.extend(
                '-A {} -d {} -j DNAT --to-destination {}'.format(
                    self.tablename, request.fake_addr, request.real_addr
                )
                for request in batch
            )
            rules.extend(('COMMIT', ''))
            command = ['iptables-restore', '--wait', '--noflush']
            try:
                result = self.iptables_runner(
                    command,
                    input='\n'.join(rules),
                    capture_output=True,
                    text=True,
                )
            except OSError as error:
                result = None
                print('ERROR: Failed to execute iptables-restore: {}'.format(error))

            success = result is not None and result.returncode == 0
            if result is not None and not success:
                print('ERROR: Failed to add {} mappings: {}'.format(
                    len(batch), result.stderr.strip()
                ))

            with self.mapping_condition:
                for request in batch:
                    self.pending_mappings.pop(request.real_addr, None)
                    if success:
                        self.ipmap[request.real_addr] = request.fake_addr
                        self.ipmap_last_used[request.real_addr] = time.monotonic()
                        print('Mapping {} to {}'.format(request.fake_addr, request.real_addr))
                    else:
                        self.unassigned_addresses.appendleft(request.fake_addr)
                    request.event.set()
                if not self.mapping_batch:
                    self.pending_mappings.clear()
                    self.mapping_batch_running = False
                    self.mapping_condition.notify_all()
                    return

    def resolve_by_asn(self, request, filtered_reply):
        address_request = request
        if request.q.qtype in (QTYPE.HTTPS, QTYPE.SVCB):
            question = DNSQuestion(request.q.qname, QTYPE.A, request.q.qclass)
            address_request = DNSRecord(header=copy(request.header), q=question, ar=request.ar)

        addresses = self.query_doh(address_request, self.resolver_client_id)
        if addresses.header.rcode != RCODE.NOERROR:
            if address_request is request:
                addresses.header.id = request.header.id
                return addresses
            if addresses.header.rcode == RCODE.NXDOMAIN:
                return filtered_reply
            reply = request.reply(aa=0)
            reply.header.rcode = addresses.header.rcode
            return reply
        hostname = str(request.q.qname)
        if not any(record.rtype == QTYPE.A and self.is_blocked_asn(str(record.rdata), hostname)
                   for record in addresses.rr):
            return filtered_reply

        if address_request is request:
            addresses.header.id = request.header.id
            return addresses
        reply = self.query_doh(request, self.resolver_client_id)
        reply.header.id = request.header.id
        return reply

    @staticmethod
    def filter_address_hint_reply(request, upstream_reply):
        """Filter HTTPS and SVCB records to remove IPv4/IPv6 address hints.

        Removes ipv4hint (key 4) and ipv6hint (key 6) SvcParamKeys from
        HTTPS and SVCB records so clients never receive real IPv4/IPv6
        addresses. Records with mandatory (key 0) requiring address hints
        or with invalid mandatory length are dropped entirely.

        If dnslib does not support parsing SVCB rdata.params, the record
        is dropped (no address hint leakage).
        """
        reply = request.reply(aa=0)
        reply.header.ad = 0
        reply.header.rcode = upstream_reply.header.rcode
        if reply.header.rcode != RCODE.NOERROR:
            return reply

        # RFC 9460 SvcParamKeys: mandatory=0, ipv4hint=4, ipv6hint=6.
        address_hints = {4, 6}
        records = []
        for record in upstream_reply.rr:
            if record.rtype not in (QTYPE.HTTPS, QTYPE.SVCB):
                continue
            data = record.rdata
            if not hasattr(data, 'params'):
                continue
            mandatory = next((value for key, value in data.params if key == 0), b'')
            if len(mandatory) % 2 or any(key in address_hints for key, in struct.iter_unpack('!H', mandatory)):
                continue
            params = [(key, value) for key, value in data.params if key not in address_hints]
            records.append(RR(record.rname, record.rtype, record.rclass, record.ttl,
                              HTTPS(data.priority, data.target, params)))

        if records:
            aliases = [record for record in upstream_reply.rr if record.rtype == QTYPE.CNAME]
            reply.add_answer(*aliases, *records)
        return reply

    # ------------------------------------------------------------------
    # IPv4 scheme (existing DNAT-based mapping)
    # ------------------------------------------------------------------

    def _ipv4_scheme(self, request, reply):
        """Apply the existing IPv4 scheme: map real A records to virtual IPs."""
        newrr = [record for record in reply.rr if record.rtype != QTYPE.CNAME]
        reply.rr = newrr
        reply.header.id = request.header.id
        real_addresses = [
            str(record.rdata)
            for record in reply.rr
            if record.rtype == QTYPE.A
        ]
        if not real_addresses:
            return reply
        mappings = self.add_mappings(real_addresses)
        for record in reply.rr:
            if record.rtype != QTYPE.A:
                continue
            real_addr = str(record.rdata)
            fake_addr = mappings.get(real_addr)
            if not fake_addr:
                err_reply = request.reply()
                err_reply.header.rcode = RCODE.SERVFAIL
                return err_reply
            record.rdata = A(fake_addr)
            record.rname = request.q.qname
            record.ttl = 300
        return reply

    # ------------------------------------------------------------------
    # IPv6 via Tundra-NAT64 (SIIT)
    # ------------------------------------------------------------------

    def _is_tundra_ready(self):
        """Return True if the Tundra-NAT64 external translation server is
        available and ready to accept translation requests.

        Checks both that the server socket is listening AND that the TUN
        interface carrier is active (carrier == 1). If the Tundra process has
        died, carrier drops to 0 and dnsmap falls back to IPv4 / SERVFAIL.
        """
        if self.tundra_server is None or not self.tundra_server.available:
            return False
        # Also verify the TUN interface is up by checking carrier.
        # When Tundra-NAT64 process dies, the TUN interface carrier becomes 0.
        try:
            carrier_path = f'/sys/class/net/{self.tundra_interface}/carrier'
            with open(carrier_path) as f:
                carrier = int(f.read().strip())
            if carrier != 1:
                return False
        except (FileNotFoundError, ValueError, OSError):
            # If we can't read carrier (e.g. outside container), fall back
            # to the available flag only.
            pass
        return True

    def _ipv6_via_tundra(self, request, handler, qname, real_aaaa_list):
        """Answer an A query with virtual IPv4(s) that Tundra translates to IPv6.

        The SIIT binding(s) and /32 route(s) are established strictly before the
        DNS answer is built, so the client never receives a blackholed VIP.
        Returns the response, or None if no VIP could be allocated (pool
        exhausted or Tundra not available) so the caller can fall back.

        Concurrent calls for the same IPv6 destination are serialized via
        pending_siit so that only one VIP is allocated and all callers
        receive the same VIP.

        Supports multiple AAAA records (CDN / round-robin): allocates a VIP
        for each unique real IPv6 and returns all corresponding A records in
        a single response.
        """
        if not self._is_tundra_ready():
            # Tundra-NAT64 is not available (no IPv6 egress, Tundra not
            # started, or socket not ready). Do not allocate a VIP or install
            # any rules/routes; let the caller fall back to IPv4 or SERVFAIL.
            return None

        # Collect unique, valid target IPv6 addresses (limit to MAX_SIIT_AAAA_RECORDS
        # to avoid CDN responses with 8–16 addresses consuming too many VIPs).
        target_ipv6s = []
        for addr in real_aaaa_list:
            try:
                ipv6 = str(IPv6Address(addr))
            except ValueError:
                continue
            if ipv6 not in target_ipv6s:
                target_ipv6s.append(ipv6)
            if len(target_ipv6s) >= MAX_SIIT_AAAA_RECORDS:
                break

        if not target_ipv6s:
            err_reply = request.reply()
            err_reply.header.rcode = RCODE.SERVFAIL
            return err_reply

        # Allocate a VIP for each target IPv6.
        vips = []
        for target_ipv6 in target_ipv6s:
            # Fast path: binding already exists.
            with self.siit_lock:
                vip = self.siit_by_real.get(target_ipv6)
                if vip is not None:
                    self.siit_last_used[target_ipv6] = time.monotonic()
                    vips.append((vip, target_ipv6))
                    continue
                # Another thread may already be allocating this VIP. Wait for it.
                event = self.pending_siit.get(target_ipv6)
                if event is not None:
                    self.siit_lock.release()
                    try:
                        event.wait()
                    finally:
                        self.siit_lock.acquire()
                    with self.siit_lock:
                        vip = self.siit_by_real.get(target_ipv6)
                    if vip is not None:
                        vips.append((vip, target_ipv6))
                    continue
                # No binding exists and no other thread is allocating one.
                event = threading.Event()
                self.pending_siit[target_ipv6] = event

            try:
                vip = self._allocate_siit_vip()
                if vip is None:
                    # Pool exhausted: do not reassign addresses that may still be
                    # present in client DNS caches. If we already have at least
                    # one VIP, return what we have instead of failing entirely.
                    if vips:
                        break
                    return None
                ok, can_return = self._setup_siit_binding(vip, target_ipv6)
                if not ok:
                    if can_return:
                        # Route failed but rollback succeeded: VIP can return to pool.
                        with self.mapping_condition:
                            self.unassigned_addresses.appendleft(vip)
                    # If can_return is False, rollback failed: VIP stays excluded.
                    if vips:
                        break
                    return None
                vips.append((vip, target_ipv6))
            finally:
                with self.siit_lock:
                    self.pending_siit.pop(target_ipv6, None)
                    event.set()

        if not vips:
            return None

        # Build a single A-record response with all allocated VIPs.
        response = request.reply()
        response.header.rcode = RCODE.NOERROR
        for vip, _ in vips:
            response.add_answer(RR(request.q.qname, QTYPE.A, ttl=300, rdata=A(vip)))
        print('IPv6 via Tundra: {} -> {} VIP(s)'.format(
            qname, len(vips)))
        return response

    # ------------------------------------------------------------------
    # Strict IPv4/IPv6 selection matrix
    # ------------------------------------------------------------------

    def _resolve_a(self, request, handler):
        """Resolve an A query according to the strict selection matrix.

            Only AAAA      -> IPv6 via Tundra (no AdGuard check)
            A and AAAA     -> AdGuard az-ipv6-* check:
                              authorized  -> IPv6 via Tundra
                              REFUSED/SERVFAIL -> existing IPv4 scheme
                              error       -> IPv4 scheme (A record exists)
            Only A         -> existing IPv4 scheme (no check)
        """
        qname = str(request.q.qname).rstrip('.').lower()

        # Resolve A via the configured (filtered) resolver and AAAA via the
        # regular recursive resolver, per the strict selection matrix.
        a_reply = self.query_doh(DNSRecord.question(qname, 'A'), self.client_id)
        # Preserve the original request's ID immediately so every return path
        # below (REFUSED, SERVFAIL, NXDOMAIN, fallback) carries the right ID.
        a_reply.header.id = request.header.id

        if a_reply.header.rcode == RCODE.REFUSED:
            reply = self.resolve_by_asn(request, a_reply)
            if (reply.header.rcode == RCODE.NOERROR
                    and any(record.rtype == QTYPE.A for record in reply.rr)):
                # resolve_by_asn found A records via ASN match → continue
                # through the general matrix (has_a → AAAA → az-ipv6-*
                # → Tundra or fallback to _ipv4_scheme).
                a_reply = reply
            elif reply.header.rcode == RCODE.SERVFAIL:
                # resolve_by_asn returned SERVFAIL → return SERVFAIL so
                # CoreDNS can try next upstream.
                return reply
            else:
                # resolve_by_asn returned REFUSED or empty NOERROR / NODATA.
                # Per-domain egress group takes precedence: 'direct' means
                # IPv4-only even for AAAA-only domains.
                if self.ipv6_egress_map.get(qname) == 'direct':
                    return a_reply
                # Try AAAA via the regular resolver: if AAAA exists, this is
                # AAAA-only and we route via Tundra without AdGuard check
                # (per strict matrix: Only AAAA -> IPv6 via Tundra, no check).
                if self._is_tundra_ready():
                    aaaa_reply = self.query_doh(DNSRecord.question(qname, 'AAAA'), self.resolver_client_id)
                    if (aaaa_reply.header.rcode == RCODE.NOERROR
                            and any(record.rtype == QTYPE.AAAA for record in aaaa_reply.rr)):
                        real_aaaa = [
                            str(record.rdata)
                            for record in aaaa_reply.rr
                            if record.rtype == QTYPE.AAAA
                        ]
                        result = self._ipv6_via_tundra(request, handler, qname, real_aaaa)
                        if result is not None:
                            return result
                        # Tundra failed; return SERVFAIL so CoreDNS can try
                        # next upstream.
                        err_reply = request.reply()
                        err_reply.header.rcode = RCODE.SERVFAIL
                        return err_reply
                # No AAAA or Tundra not available → return original REFUSED
                # so CoreDNS can try next upstream.
                return a_reply
        if a_reply.header.rcode != RCODE.NOERROR:
            return a_reply

        has_a = any(record.rtype == QTYPE.A for record in a_reply.rr)

        if not has_a:
            # AAAA-only: successful NOERROR answer without A records.
            # Per-domain egress group takes precedence: 'direct' means
            # IPv4-only even for AAAA-only domains.
            if self.ipv6_egress_map.get(qname) == 'direct':
                return a_reply
            if self.tundra_server is None:
                return a_reply
            aaaa_reply = self.query_doh(DNSRecord.question(qname, 'AAAA'), self.resolver_client_id)
            if (aaaa_reply.header.rcode == RCODE.NOERROR
                    and any(record.rtype == QTYPE.AAAA for record in aaaa_reply.rr)):
                real_aaaa = [
                    str(record.rdata)
                    for record in aaaa_reply.rr
                    if record.rtype == QTYPE.AAAA
                ]
                result = self._ipv6_via_tundra(request, handler, qname, real_aaaa)
                if result is not None:
                    return result
                err_reply = request.reply()
                err_reply.header.rcode = RCODE.SERVFAIL
                return err_reply
            # No A and no AAAA: return the A-query answer (correct QTYPE in
            # Question section) with the original request ID.
            return a_reply

        # We have A records; check whether AAAA also exists.
        # If Tundra is not available, skip AAAA lookup and go straight to IPv4.
        if not self._is_tundra_ready():
            return self._ipv4_scheme(request, a_reply)

        try:
            aaaa_reply = self.query_doh(DNSRecord.question(qname, 'AAAA'), self.resolver_client_id)
        except Exception as error:
            print('DNS: AAAA query failed for {}, falling back to IPv4: {}'.format(qname, error))
            return self._ipv4_scheme(request, a_reply)
        has_aaaa = (aaaa_reply.header.rcode == RCODE.NOERROR
                    and any(record.rtype == QTYPE.AAAA for record in aaaa_reply.rr))

        if not has_aaaa:
            # Only A: no AdGuard check, existing IPv4 scheme.
            return self._ipv4_scheme(request, a_reply)

        # A and AAAA: decide between IPv6 via Tundra and IPv4 scheme.
        # Per-domain egress group takes precedence: 'direct' always means
        # IPv4-only, even when aaaa_priority is enabled.
        egress_group = self.ipv6_egress_map.get(qname)
        if egress_group == 'direct':
            return self._ipv4_scheme(request, a_reply)

        if self.aaaa_priority and self._is_tundra_ready():
            # AAAA priority: prefer IPv6 via Tundra for dual-stack domains
            # without requiring AdGuard az-ipv6-* authorization.
            real_aaaa = [
                str(record.rdata)
                for record in aaaa_reply.rr
                if record.rtype == QTYPE.AAAA
            ]
            result = self._ipv6_via_tundra(request, handler, qname, real_aaaa)
            if result is not None:
                return result
            # Tundra failed; fall through to IPv4 scheme.
        else:
            # AdGuard az-ipv6-* authorization decides.
            check = self.check_ipv6_authorized(qname)
            if check == IPV6_EGRESS_AUTHORIZED:
                real_aaaa = [
                    str(record.rdata)
                    for record in aaaa_reply.rr
                    if record.rtype == QTYPE.AAAA
                ]
                result = self._ipv6_via_tundra(request, handler, qname, real_aaaa)
                if result is not None:
                    return result
        # DENIED, technical error, or Tundra failure: fall back to IPv4 scheme.
        return self._ipv4_scheme(request, a_reply)

    def _resolve_https(self, request):
        reply = self.query_doh(request, self.client_id)
        if reply.header.rcode == RCODE.REFUSED:
            reply = self.resolve_by_asn(request, reply)
        return self.filter_address_hint_reply(request, reply)

    def resolve(self, request, handler):
        try:
            qname = str(request.q.qname).rstrip('.')
            qtype = request.q.qtype

            # Never leak real IPv6 addresses: AAAA queries get an empty NOERROR.
            # IPv6 destinations are reached through a virtual IPv4 + Tundra-NAT64.
            if qtype == QTYPE.AAAA:
                return request.reply()

            # Local healthcheck probe answered directly without upstream.
            if qtype == QTYPE.A and qname.lower() == 'healthcheck.invalid':
                client_address = self._client_address(handler)
                if client_address == '127.0.0.1':
                    reply = request.reply()
                    reply.add_answer(RR(qname, QTYPE.A, rdata=A('127.0.0.1')))
                    return reply

            if qtype == QTYPE.HTTPS or qtype == QTYPE.SVCB:
                return self._resolve_https(request)

            if qtype == QTYPE.A:
                return self._resolve_a(request, handler)

            reply = self.query_doh(request, self.client_id)
            reply.header.id = request.header.id
            return reply

        except Exception as error:
            self.log_processing_error(error)
            reply = request.reply()
            reply.header.rcode = RCODE.SERVFAIL
            return reply


class PassthroughDNSHandler(DNSHandler):
    """
        Modify DNSHandler logic (get_reply method) to send directly to
        upstream DNS server rather then decoding/encoding packet and
        passing to Resolver (The request/response packets are still
        parsed and logged but this is not inline)
    """
    def get_reply(self,data):
        host,port = self.server.resolver.address,self.server.resolver.port

        request = DNSRecord.parse(data)
        self.log_request(request)

        if self.protocol == 'tcp':
            data = struct.pack("!H",len(data)) + data
            response = send_tcp(data,host,port)
            response = response[2:]
        else:
            response = send_udp(data,host,port)

        reply = DNSRecord.parse(response)
        self.log_reply(reply)

        return response


def send_tcp(data,host,port):
    """
        Helper function to send/receive DNS TCP request
        (in/out packets will have prepended TCP length header)
    """
    sock = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    sock.connect((host,port))
    sock.sendall(data)
    response = sock.recv(8192)
    length = struct.unpack("!H",bytes(response[:2]))[0]
    while len(response) - 2 < length:
        response += sock.recv(8192)
    sock.close()
    return response


def send_udp(data,host,port):
    """
        Helper function to send/receive DNS UDP request
    """
    sock = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    sock.sendto(data,(host,port))
    response,server = sock.recvfrom(8192)
    sock.close()
    return response


if __name__ == '__main__':

    import argparse, time, os, sys
    from pathlib import Path

    flag_file_path = '/dev/shm/.dns_started'

    dns = os.getenv('DNS', '127.0.0.1') + ':53'

    p = argparse.ArgumentParser(description="DNS Proxy")

    p.add_argument("--port", "-p", type=int, default=53,
                   metavar="<port>",
                   help="Local proxy port (default: 53)")
    p.add_argument("--address","-a", default="",
                   metavar="<address>",
                   help="Local proxy listen address (default: all)")
    p.add_argument("--upstream","-u", default=dns,
                   metavar="<dns server:port>",
                   help=f"Upstream DNS server:port (default: {dns})")
    p.add_argument("--doh-port", type=int, default=3000,
                   help="AdGuard unencrypted DNS-over-HTTPS port (default: 3000)")
    p.add_argument("--tcp", action='store_true', default=False,
                   help="TCP proxy (default: UDP only)")
    p.add_argument("--timeout","-o", type=float, default=5,
                   metavar="<timeout>",
                   help="Upstream timeout (default: 5s)")
    p.add_argument("--passthrough", action='store_true', default=False,
                   help="Dont decode/re-encode request/response (default: off)")
    p.add_argument("--log", default="request,reply,truncated,error",
                   help="Log hooks to enable (default: +request,+reply,+truncated,+error,-recv,-send,-data)")
    p.add_argument("--log-prefix", action='store_true', default=False,
                   help="Log prefix (timestamp/handler/resolver) (default: False)")
    p.add_argument("--iprange", default="14.16.0.0/16",
                   metavar="<ip/mask>",
                   help="Fake IP range (default: 14.16.0.0/16)")
    p.add_argument("--client-id", default=os.getenv('CLIENT', 'az-local'),
                   help="AdGuard client ID used for filtered requests")
    p.add_argument("--resolver-client-id", default="az-resolver",
                   help="AdGuard client ID used to resolve REFUSED responses")
    p.add_argument("--asn-file", default="/root/antizapret/result/asn.txt",
                   help="Semicolon-separated files with blocked ASN numbers and organization names")
    p.add_argument("--asn-database", default="/usr/share/GeoIP/GeoLite2-ASN.mmdb",
                   help="MaxMind ASN database")
    p.add_argument("--tundra-socket", default=None,
                   metavar="<path>",
                   help="Tundra-NAT64 external mapping protocol Unix socket (IPv6 egress via SIIT)")
    p.add_argument("--tundra-interface", default="tundra",
                   metavar="<iface>",
                   help="Tundra TUN interface for VIP /32 routes (default: tundra)")
    p.add_argument("--ipv6-client-id", default=None,
                   metavar="<client-id>",
                   help="AdGuard ClientID for IPv6 egress authorization (e.g. az-ipv6-local)")
    p.add_argument("--excluded-addresses", default="",
                   metavar="<addrs>",
                   help="Comma-separated IPv4 addresses excluded from the VIP pool "
                        "(Tundra router, DNS fallback, etc.)")
    p.add_argument("--tundra-router-ipv4", default=TUNDRA_ROUTER_IPV4,
                   metavar="<addr>",
                   help="Tundra-NAT64 router IPv4 address used as fallback for "
                        "unknown ICMPv6 sources (default: {})".format(TUNDRA_ROUTER_IPV4))
    p.add_argument("--vip-stale-ttl", type=int, default=7200,
                   metavar="<seconds>",
                   help="Seconds after which an unused VIP may be evicted "
                        "when the pool is exhausted (default: 7200)")
    p.add_argument("--vip-cooldown-ttl", type=int, default=300,
                   metavar="<seconds>",
                   help="Seconds an evicted VIP is quarantined before reuse "
                        "(default: 300)")
    p.add_argument("--aaaa-priority", action='store_true', default=False,
                   help="Prefer AAAA over A for dual-stack domains: route via "
                        "Tundra-NAT64 using the AAAA address without requiring "
                        "AdGuard az-ipv6-* authorization (default: False)")
    p.add_argument("--ipv6-egress-map", default="",
                   metavar="<map>",
                   help="Comma-separated domain:egress_group pairs for per-domain "
                        "IPv6 egress selection. egress_group: local, world, direct "
                        "(default: empty, all domains use default client ID)")
    args = p.parse_args()

    args.dns,_,args.dns_port = args.upstream.partition(':')
    args.dns_port = int(args.dns_port or 53)

    print("Starting Proxy Resolver (%s:%d -> %s:%d) [%s]" % (
          args.address or "*",args.port,
          args.dns,args.dns_port,
          "UDP/TCP" if args.tcp else "UDP"))

    excluded = [a for a in args.excluded_addresses.split(',') if a.strip()]
    # Parse per-domain IPv6 egress map: domain:group,domain:group,...
    ipv6_egress_map = {}
    for entry in args.ipv6_egress_map.split(','):
        entry = entry.strip()
        if not entry:
            continue
        if ':' not in entry:
            print('WARNING: ignoring invalid ipv6-egress-map entry: {}'.format(entry))
            continue
        domain, group = entry.split(':', 1)
        domain = domain.strip().rstrip('.').lower()
        group = group.strip()
        if group not in ('local', 'world', 'direct'):
            print('WARNING: ignoring invalid egress group {} for {} (must be local/world/direct)'.format(
                group, domain))
            continue
        ipv6_egress_map[domain] = group
    resolver = ProxyResolver(args.dns,args.dns_port,args.doh_port,args.timeout,args.iprange,
                             args.client_id,args.resolver_client_id,
                             args.asn_file,args.asn_database,
                             tundra_socket=args.tundra_socket,
                             tundra_interface=args.tundra_interface,
                             ipv6_client_id=args.ipv6_client_id,
                             excluded_addresses=excluded,
                             tundra_router_ipv4=args.tundra_router_ipv4,
                             vip_stale_ttl=args.vip_stale_ttl,
                             vip_cooldown_ttl=args.vip_cooldown_ttl,
                             aaaa_priority=args.aaaa_priority,
                             ipv6_egress_map=ipv6_egress_map)
    atexit.register(resolver.close)
    signal.signal(signal.SIGHUP, resolver.request_asn_reload)
    handler = PassthroughDNSHandler if args.passthrough else DNSHandler
    logger = DNSLogger(args.log,args.log_prefix)
    udp_server = DNSServer(resolver,
                           port=args.port,
                           address=args.address,
                           logger=logger,
                           handler=handler)
    udp_server.start_thread()

    if args.tcp:
        tcp_server = DNSServer(resolver,
                               port=args.port,
                               address=args.address,
                               tcp=True,
                               logger=logger,
                               handler=handler)
        tcp_server.start_thread()

    Path(flag_file_path).touch()
    while udp_server.isAlive():
        time.sleep(1)
