#!/usr/bin/env -S python3 -u
# -*- coding: utf-8 -*-

from __future__ import print_function

import base64, os, signal, socket, struct, subprocess, threading
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from collections import deque
from ipaddress import IPv4Network

import maxminddb

from dnslib import DNSRecord, RCODE, QTYPE, A
from dnslib.server import DNSServer, DNSHandler, BaseResolver, DNSLogger


class ProxyResolver(BaseResolver):
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

    def __init__(self,address,port,doh_port,timeout,iprange,client_id,
                 resolver_client_id,asn_file,asn_database,tablename='dnsmap'):
        self.address = address
        self.port = port
        self.timeout = timeout
        self.doh_url = 'http://{}:{}/dns-query'.format(address, doh_port)
        self.client_id = client_id
        self.resolver_client_id = resolver_client_id
        self.asn_file = asn_file
        self.blocked_asns = set()
        self.blocked_organizations = set()
        self.asn_list_lock = threading.RLock()
        self.asn_reload_requested = threading.Event()
        self.reload_asn_list()
        self.asn_database = maxminddb.open_database(asn_database)
        self.unassigned_addresses = deque([str(x) for x in IPv4Network(iprange).hosts()])
        # preserve first address from range for DNS
        del self.unassigned_addresses[0]

        self.ipmap = {}
        self.tablename = tablename
        self.mapping_lock = threading.RLock()

        # Load existing mappings
        get_mappings = "iptables -w -t nat -nL dnsmap | awk '{if (NR<3) {next}; sub(/to:/, \"\", $6); print $5,$6}'"
        output = subprocess.check_output(get_mappings, shell=True, encoding='utf-8')
        for mapped in output.split("\n"):
            if mapped:
                fake_addr, real_addr = mapped.split(' ')
                if not self.add_mapping(real_addr, fake_addr):
                    print("ERROR: Failed to load mapping {} to {}, ignoring".format(fake_addr, real_addr))
        #self.unassigned_addresses.remove()

    @staticmethod
    def normalize_organization(value):
        return ''.join(character for character in value.casefold() if character.isalnum())

    @classmethod
    def load_asn_list(cls, path):
        asns = set()
        organizations = set()
        try:
            with open(path, encoding='utf-8') as asn_list:
                for raw_line in asn_list:
                    line = raw_line.split('#', 1)[0].strip()
                    if not line:
                        continue
                    normalized_asn = line.upper()
                    if normalized_asn.startswith('AS') and normalized_asn[2:].isdigit():
                        asns.add(int(normalized_asn[2:]))
                    elif line.isdigit():
                        asns.add(int(line))
                    else:
                        organization = cls.normalize_organization(line)
                        if organization:
                            organizations.add(organization)
        except FileNotFoundError:
            print('ASN list {} not found, continuing with an empty list'.format(path))
        print('Loaded {} ASN numbers and {} organization names'.format(len(asns), len(organizations)))
        return asns, organizations

    def reload_asn_list(self):
        with self.asn_list_lock:
            self.asn_reload_requested.clear()
            asns, organizations = self.load_asn_list(self.asn_file)
            self.blocked_asns = asns
            self.blocked_organizations = organizations

    def request_asn_reload(self, signum, frame):
        print('Received SIGHUP, ASN list will be reloaded')
        self.asn_reload_requested.set()

    def query_doh(self, request, client_id):
        dns = base64.urlsafe_b64encode(request.pack()).rstrip(b'=').decode('ascii')
        url = '{}/{}?dns={}'.format(self.doh_url, quote(client_id, safe=''), dns)
        http_request = Request(url, headers={'Accept': 'application/dns-message'})
        with urlopen(http_request, timeout=self.timeout) as response:
            return DNSRecord.parse(response.read())

    def is_blocked_asn(self, address):
        if self.asn_reload_requested.is_set():
            self.reload_asn_list()
        record = self.asn_database.get(address)
        if not record:
            return False
        asn = record.get('autonomous_system_number')
        organization = self.normalize_organization(record.get('autonomous_system_organization') or '')
        blocked = asn in self.blocked_asns or any(
            name in organization for name in self.blocked_organizations
        )
        if blocked:
            print('ASN match for {}: AS{} {}'.format(address, asn, record.get('autonomous_system_organization', '')))
        return blocked

    def get_mapping(self, real_addr):
        return self.ipmap.get(real_addr)

    def add_mapping(self, real_addr, fake_addr=None):
        with self.mapping_lock:
            existing_fake_addr = self.get_mapping(real_addr)
            if existing_fake_addr:
                if fake_addr:
                    print("ERROR: Real addr {} is already mapped to {}, ignoring duplicate mapping to {}".format(
                        real_addr, existing_fake_addr, fake_addr
                    ))
                    return True
                return existing_fake_addr

            if fake_addr:
                try:
                    self.unassigned_addresses.remove(fake_addr)
                    self.ipmap[real_addr]=fake_addr
                    print('Mapping {} to {}'.format(fake_addr, real_addr))
                except ValueError:
                    print("ERROR: Fake addr {} not in unassigned addresses list".format(fake_addr))
                    return False
            else:
                try:
                    fake_addr = self.unassigned_addresses.popleft()
                except IndexError:
                    print("ERROR: No IP addresses left!!!")
                    return False
                print('Mapping {} to {}'.format(fake_addr, real_addr))
                self.ipmap[real_addr]=fake_addr
                set_mappings = f"iptables -w -t nat -A dnsmap -d '{fake_addr}' -j DNAT --to '{real_addr}'"
                subprocess.call(set_mappings, shell=True, encoding='utf-8')
                return fake_addr
            return True

    def resolve(self,request,handler):
        try:
            reply = self.query_doh(request, self.client_id)

            if request.q.qtype == QTYPE.AAAA or request.q.qtype == QTYPE.HTTPS:
                print('GOT AAAA or HTTPS')
                reply = request.reply()
                return reply

            if request.q.qtype == QTYPE.A:
                print('GOT A')

                if reply.header.rcode == RCODE.SERVFAIL:
                    filtered_reply = reply
                    resolved_reply = self.query_doh(request, self.resolver_client_id)
                    real_addresses = [
                        str(record.rdata)
                        for record in resolved_reply.rr
                        if record.rtype == QTYPE.A
                    ]
                    if not any(self.is_blocked_asn(address) for address in real_addresses):
                        return filtered_reply
                    reply = resolved_reply

                newrr = []
                for record in reply.rr:
                    if record.rtype == QTYPE.CNAME:
                        continue
                    newrr.append(record)
                reply.rr = newrr

                for record in reply.rr:
                    if record.rtype != QTYPE.A:
                        continue

                    # print(dir(record))
                    # print(type(record.rdata))

                    real_addr = str(record.rdata)
                    fake_addr = self.get_mapping(real_addr)
                    if not fake_addr:
                        fake_addr = self.add_mapping(real_addr)
                    if not fake_addr:
                        print("No fake_addr, something went wrong!")
                        reply = request.reply()
                        reply.header.rcode = getattr(RCODE,'SERVFAIL')
                        return reply

                    record.rdata = A(fake_addr)
                    record.rname = request.q.qname
                    record.ttl = 300
                    # print(a.rdata)
                return reply

            # print(reply)
        except (socket.timeout, TimeoutError, HTTPError, URLError) as error:
            print('DNS-over-HTTPS request failed: {}'.format(error))
            reply = request.reply()
            reply.header.rcode = getattr(RCODE,'SERVFAIL')

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

    flag_file_path = '/tmp/.dns_started'
    Path(flag_file_path).unlink(missing_ok=True)

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
                   help="AdGuard client ID used to resolve SERVFAIL responses")
    p.add_argument("--asn-file", default="/root/antizapret/result/asn.txt",
                   help="Blocked ASN numbers and organization names")
    p.add_argument("--asn-database", default="/usr/share/GeoIP/GeoLite2-ASN.mmdb",
                   help="MaxMind ASN database")
    args = p.parse_args()

    args.dns,_,args.dns_port = args.upstream.partition(':')
    args.dns_port = int(args.dns_port or 53)

    print("Starting Proxy Resolver (%s:%d -> %s:%d) [%s]" % (
          args.address or "*",args.port,
          args.dns,args.dns_port,
          "UDP/TCP" if args.tcp else "UDP"))

    resolver = ProxyResolver(args.dns,args.dns_port,args.doh_port,args.timeout,args.iprange,
                             args.client_id,args.resolver_client_id,
                             args.asn_file,args.asn_database)
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
