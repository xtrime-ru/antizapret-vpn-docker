#!/usr/bin/env -S python3 -u
# -*- coding: utf-8 -*-

from __future__ import print_function

import atexit, base64, http.client, os, queue, re, signal, socket, struct, subprocess, threading, time, traceback
from urllib.parse import quote

from collections import deque, namedtuple
from ipaddress import IPv4Network

import maxminddb

from dnslib import DNSRecord, RCODE, QTYPE, A
from dnslib.server import DNSServer, DNSHandler, BaseResolver, DNSLogger


AsnMatchRules = namedtuple('AsnMatchRules', ('asn_rules', 'substring_rules', 'regex_rules'))


class DoHError(Exception):
    pass


class DoHResponseError(DoHError):
    pass


class DoHProtocolError(DoHError):
    pass


class ProxyResolver(BaseResolver):
    MAX_DOH_RESPONSE_SIZE = 65535
    ERROR_LOG_INTERVAL = 5
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
                 resolver_client_id,asn_file,asn_database,tablename='dnsmap',
                 asn_database_reader=None, iptables_runner=None,
                 connection_factory=None, load_existing_mappings=True):
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
        self.unassigned_addresses = deque([str(x) for x in IPv4Network(iprange).hosts()])
        # preserve first address from range for DNS
        del self.unassigned_addresses[0]

        self.ipmap = {}
        self.tablename = tablename
        self.mapping_lock = threading.RLock()

        # Load existing mappings
        if load_existing_mappings:
            get_mappings = "iptables -w -t nat -nL dnsmap | awk '{if (NR<3) {next}; sub(/to:/, \"\", $6); print $5,$6}'"
            output = subprocess.check_output(get_mappings, shell=True, encoding='utf-8')
            for mapped in output.split("\n"):
                if mapped:
                    fake_addr, real_addr = mapped.split(' ')
                    if not self.add_mapping(real_addr, fake_addr):
                        print("ERROR: Failed to load mapping {} to {}, ignoring".format(fake_addr, real_addr))
        #self.unassigned_addresses.remove()

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

    @classmethod
    def load_asn_list(cls, path):
        asn_rules = {}
        substring_rules = []
        regex_rules = []
        with open(path, encoding='utf-8') as asn_list:
            for raw_line in asn_list:
                line = raw_line.split('#', 1)[0].strip()
                if not line:
                    continue
                normalized_asn = line.upper()
                if normalized_asn.startswith('AS') and normalized_asn[2:].isdigit():
                    asn = int(normalized_asn[2:])
                    asn_rules[asn] = line
                elif line.isdigit():
                    asn = int(line)
                    asn_rules[asn] = line
                elif line.startswith('/') and line.endswith('/'):
                    regex_rules.append(cls.organization_regex_rule(line))
                else:
                    substring_rules.append(cls.organization_substring_rule(line))
        return AsnMatchRules(asn_rules, substring_rules, regex_rules)

    def reload_asn_list(self):
        with self.asn_list_lock:
            rules = self.load_asn_list(self.asn_file)
            self.asn_match_rules = rules
            return len(rules.asn_rules), len(rules.substring_rules) + len(rules.regex_rules)

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
        while True:
            try:
                self.doh_connections.get_nowait().close()
            except queue.Empty:
                break
        close_database = getattr(self.asn_database, 'close', None)
        if close_database:
            close_database()

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

    def is_blocked_asn(self, address):
        record = self.asn_database.get(address)
        if not record:
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
        if rule:
            print('ASN match for {}: AS{}; organization: {}; rule: {}'.format(address, asn, raw_organization, rule))
            return True
        return False

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
                command = [
                    'iptables', '-w', '-t', 'nat', '-A', self.tablename,
                    '-d', fake_addr, '-j', 'DNAT', '--to', real_addr,
                ]
                try:
                    result = self.iptables_runner(command, capture_output=True, text=True)
                except OSError as error:
                    self.unassigned_addresses.appendleft(fake_addr)
                    print('ERROR: Failed to execute iptables: {}'.format(error))
                    return False
                if result.returncode != 0:
                    self.unassigned_addresses.appendleft(fake_addr)
                    print('ERROR: Failed to add mapping {} to {}: {}'.format(
                        fake_addr, real_addr, result.stderr.strip()
                    ))
                    return False
                print('Mapping {} to {}'.format(fake_addr, real_addr))
                self.ipmap[real_addr]=fake_addr
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
        except Exception as error:
            self.log_processing_error(error)
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
