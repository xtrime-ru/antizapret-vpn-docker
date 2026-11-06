#!/usr/bin/env -S python3 -u
# -*- coding: utf-8 -*-

from __future__ import print_function

import atexit, base64, http.client, os, queue, re, signal, socket, struct, subprocess, threading, time, traceback
from urllib.parse import quote

from collections import deque, namedtuple
from ipaddress import IPv4Address, IPv4Network, IPv6Address

import maxminddb

from dnslib import DNSRecord, RCODE, QTYPE, A, AAAA, RR
from dnslib.server import DNSServer, DNSHandler, BaseResolver, DNSLogger


AsnMatchRules = namedtuple('AsnMatchRules', ('asn_rules', 'substring_rules', 'regex_rules'))
MappingRequest = namedtuple('MappingRequest', ('real_addr', 'fake_addr', 'event'))
IPv6MappingRequest = namedtuple('IPv6MappingRequest', ('real_addr', 'fake_addr', 'event'))


# IPv6 egress authorization result codes
IPV6_EGRESS_AUTHORIZED = 1   # AdGuard explicitly authorized the domain
IPV6_EGRESS_DENIED = 2       # AdGuard explicitly denied (SERVFAIL catch-all)
IPV6_EGRESS_ERROR = 3        # Technical error (timeout, connection failure, etc.)


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

    def __init__(self,address,port,doh_port,timeout,iprange,client_id,
                 resolver_client_id,asn_file,asn_database,tablename='dnsmap',
                 asn_database_reader=None, iptables_runner=None,
                 connection_factory=None, load_existing_mappings=True,
                 mapping_batch_delay=MAPPING_BATCH_DELAY,
                 ipv6_egress_file=None, ipv6_translator_port=9000,
                 ipv6_egress_client_id=None, ipv6_egress_enabled=False,
                 ipv6_mapping_file=None, ipv6_tablename='dnsmap-ipv6',
                 ipv6_egress_mode='adguard_only',
                 ipv6_egress_reserve_count=100):
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
        self.unassigned_addresses = self.new_unassigned_addresses()
        self.mapping_capacity = len(self.unassigned_addresses)

        self.ipmap = {}
        self.tablename = tablename
        self.mapping_lock = threading.RLock()
        self.mapping_condition = threading.Condition(self.mapping_lock)
        # Separate state for IPv4 and IPv6 mapping handlers to prevent
        # cross-contamination: IPv4 addresses must never appear in IPv6
        # iptables rules and vice versa.
        self.ipv4_pending_mappings = {}
        self.ipv6_pending_mappings = {}
        self.ipv4_mapping_batch = []
        self.ipv6_mapping_batch = []
        self.mapping_batch_delay = mapping_batch_delay
        self.ipv4_mapping_batch_running = False
        self.ipv6_mapping_batch_running = False
        self.mapping_stopping = False

        # IPv6 egress support
        self.ipv6_egress_file = ipv6_egress_file
        self.ipv6_translator_port = ipv6_translator_port
        self.ipv6_egress_client_id = ipv6_egress_client_id
        self.ipv6_egress_enabled = ipv6_egress_enabled
        self.ipv6_mapping_file = ipv6_mapping_file or '/root/antizapret/result/ipv6-egress-mappings.txt'
        self.ipv6_tablename = ipv6_tablename
        self.ipv6_egress_mode = ipv6_egress_mode
        self.ipv6_egress_reserve_count = ipv6_egress_reserve_count
        self.ipv6_egress_domains = set()
        self.ipv6_mappings = {}  # fake_ipv4 -> real_ipv6
        self.ipv6_domains_lock = threading.RLock()
        self.ipv6_connectivity_checked = False
        self.ipv6_connectivity_available = False

        # Separate address pool for IPv6 egress to prevent pool exhaustion
        # from destroying regular IPv4 mappings.
        self.ipv6_unassigned_addresses = deque()
        self._reserve_ipv6_addresses()
        if ipv6_egress_file:
            self.load_ipv6_egress_domains()
            print('IPv6 egress domains loaded: {}'.format(
                ', '.join(sorted(self.ipv6_egress_domains)) if self.ipv6_egress_domains else '(none)'
            ))
        if ipv6_egress_client_id:
            print('IPv6 egress AdGuard ClientID: {}'.format(ipv6_egress_client_id))

        # Load existing IPv6 egress mappings from the mapping file. This
        # restores fake IPv4 -> real IPv6 correspondences after a container
        # restart, so that the translator and NAT rules remain consistent.
        restored_ipv6 = self.load_ipv6_mappings()

        # Create NAT rules for restored IPv6 mappings so that traffic to the
        # virtual IPv4 addresses is redirected to the translator.
        if restored_ipv6:
            rules = ['*nat']
            rules.extend(
                '-A {} -p {} -d {} -j DNAT --to-destination 127.0.0.1:{}'.format(
                    self.ipv6_tablename, protocol, fake_ipv4, self.ipv6_translator_port
                )
                for fake_ipv4, real_ipv6 in restored_ipv6
                for protocol in ('tcp', 'udp')
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
                if result is not None and result.returncode != 0:
                    print('ERROR: Failed to restore {} IPv6 mappings: {}'.format(
                        len(restored_ipv6), result.stderr.strip()
                    ))
            except OSError as error:
                print('ERROR: Failed to execute iptables-restore for IPv6 restore: {}'.format(error))

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
        paths = [item.strip() for item in str(path).split(';') if item.strip()]
        for asn_path in paths:
            with open(asn_path, encoding='utf-8') as asn_list:
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

    # ------------------------------------------------------------------
    # IPv6 egress domain support
    # ------------------------------------------------------------------

    def load_ipv6_egress_domains(self):
        """Load domains configured for IPv6 egress from the config file."""
        if not self.ipv6_egress_file:
            return
        try:
            with open(self.ipv6_egress_file, encoding='utf-8') as f:
                domains = set()
                for raw_line in f:
                    line = raw_line.split('#', 1)[0].strip()
                    if not line:
                        continue
                    # Normalize: lowercase, strip trailing dot
                    domain = line.lower().rstrip('.')
                    if domain:
                        domains.add(domain)
            with self.ipv6_domains_lock:
                self.ipv6_egress_domains = domains
            print('Loaded {} IPv6 egress domain(s) from {}'.format(
                len(domains), self.ipv6_egress_file
            ))
        except FileNotFoundError:
            print('IPv6 egress file not found: {} (no domains configured)'.format(
                self.ipv6_egress_file
            ))
            with self.ipv6_domains_lock:
                self.ipv6_egress_domains = set()
        except Exception as error:
            print('Failed to load IPv6 egress domains: {}'.format(error))

    def reload_ipv6_egress_domains(self, signum, frame):
        """Reload IPv6 egress domains on SIGHUP."""
        try:
            self.load_ipv6_egress_domains()
        except Exception as error:
            os.write(2, 'Failed to reload IPv6 egress domains: {}\n'.format(
                error
            ).encode())

    def load_ipv6_mappings(self):
        """Load existing IPv6 egress mappings from the mapping file.

        Reads the mapping file (fake_ipv4 real_ipv6 format), validates each
        entry, and populates ipv6_mappings and ipv6_unassigned_addresses
        accordingly. Returns a list of (fake_ipv4, real_ipv6) tuples for NAT
        rule creation.

        Duplicate fake IPv4 or real IPv6 addresses are skipped (first
        occurrence wins). Entries with fake IPv4 outside the IPv6 egress pool
        or invalid addresses are skipped with a diagnostic message.
        """
        if not self.ipv6_mapping_file:
            return []

        restored = []
        seen_fake = set()
        seen_real = set()

        try:
            with open(self.ipv6_mapping_file, 'r', encoding='utf-8') as f:
                for line_num, raw_line in enumerate(f, 1):
                    line = raw_line.strip()
                    if not line or line.startswith('#'):
                        continue

                    parts = line.split()
                    if len(parts) < 2:
                        print('IPv6 egress: skipping malformed line {}: {}'.format(
                            line_num, line
                        ))
                        continue

                    fake_ipv4 = parts[0]
                    real_ipv6 = parts[1]

                    # Validate fake IPv4 address
                    try:
                        IPv4Address(fake_ipv4)
                    except ValueError:
                        print('IPv6 egress: skipping invalid fake IPv4 {} on line {}'.format(
                            fake_ipv4, line_num
                        ))
                        continue

                    # Validate real IPv6 address
                    try:
                        IPv6Address(real_ipv6)
                    except ValueError:
                        print('IPv6 egress: skipping invalid real IPv6 {} on line {}'.format(
                            real_ipv6, line_num
                        ))
                        continue

                    # Check fake IPv4 is in the IPv6 egress pool
                    if fake_ipv4 not in self.ipv6_unassigned_addresses:
                        print('IPv6 egress: skipping fake IPv4 {} not in egress pool on line {}'.format(
                            fake_ipv4, line_num
                        ))
                        continue

                    # Skip duplicates
                    if fake_ipv4 in seen_fake:
                        print('IPv6 egress: skipping duplicate fake IPv4 {} on line {}'.format(
                            fake_ipv4, line_num
                        ))
                        continue
                    if real_ipv6 in seen_real:
                        print('IPv6 egress: skipping duplicate real IPv6 {} on line {}'.format(
                            real_ipv6, line_num
                        ))
                        continue

                    seen_fake.add(fake_ipv4)
                    seen_real.add(real_ipv6)

                    # Remove from unassigned pool so it cannot be re-allocated
                    self.ipv6_unassigned_addresses.remove(fake_ipv4)

                    # Store mapping
                    self.ipv6_mappings[real_ipv6] = fake_ipv4
                    restored.append((fake_ipv4, real_ipv6))

            print('IPv6 egress: loaded {} mapping(s) from {}'.format(
                len(restored), self.ipv6_mapping_file
            ))
            return restored

        except FileNotFoundError:
            print('IPv6 egress mapping file not found: {} (no mappings restored)'.format(
                self.ipv6_mapping_file
            ))
            return []
        except OSError as error:
            print('IPv6 egress: failed to read mapping file: {}'.format(error))
            return []

    def is_ipv6_egress_domain(self, domain):
        """Check if a domain (or any parent domain) is configured for IPv6 egress."""
        if not domain:
            return False
        domain = domain.lower().rstrip('.')
        with self.ipv6_domains_lock:
            # Check exact match first
            if domain in self.ipv6_egress_domains:
                return True
            # Check parent domains (subdomain matching)
            parts = domain.split('.')
            for i in range(1, len(parts)):
                parent = '.'.join(parts[i:])
                if parent in self.ipv6_egress_domains:
                    return True
        return False

    # ------------------------------------------------------------------
    # AdGuard Home integration for IPv6 egress domain selection
    # ------------------------------------------------------------------

    def check_ipv6_egress_via_adguard(self, domain):
        """Check if a domain is authorized for IPv6 egress via AdGuard Home.

        Queries AdGuard Home with the IPv6 egress ClientID (e.g. az-ipv6-local).
        Returns one of:
          IPV6_EGRESS_AUTHORIZED - domain matched an explicit allow rule (NOERROR with A record)
          IPV6_EGRESS_DENIED     - domain matched catch-all SERVFAIL (explicit denial)
          IPV6_EGRESS_ERROR      - technical failure (timeout, connection error, unexpected rcode)

        A technical error is NOT treated as denial, so callers can decide
        whether to fall back to file-based configuration.
        """
        if not self.ipv6_egress_client_id or not self.ipv6_egress_enabled:
            return IPV6_EGRESS_ERROR

        try:
            # Query AdGuard Home with the IPv6 egress ClientID
            a_request = DNSRecord.question(domain, 'A')
            reply = self.query_doh(a_request, self.ipv6_egress_client_id)

            if reply.header.rcode == RCODE.NOERROR:
                # Check if there are any answer records (not just SOA/authority)
                for record in reply.rr:
                    if record.rtype == QTYPE.A:
                        # Domain matched a specific IPv6 egress rule with A record
                        print('IPv6 egress: AdGuard authorized {} via ClientID {} (A={})'.format(
                            domain, self.ipv6_egress_client_id, record.rdata
                        ))
                        return IPV6_EGRESS_AUTHORIZED
                # NOERROR without A records: domain is authorized by AdGuard
                # (catch-all SERVFAIL did not match) but has no A record -
                # likely AAAA-only. Proceed to IPv6 egress resolution which
                # will look up AAAA records via resolve_cname_chain().
                print('IPv6 egress: AdGuard NOERROR without A for {} via ClientID {} - authorized (AAAA-only possible)'.format(
                    domain, self.ipv6_egress_client_id
                ))
                return IPV6_EGRESS_AUTHORIZED
            elif reply.header.rcode == RCODE.SERVFAIL:
                # Catch-all SERVFAIL rule matched - domain is explicitly NOT for IPv6 egress
                print('IPv6 egress: AdGuard SERVFAIL for {} via ClientID {} - explicitly denied'.format(
                    domain, self.ipv6_egress_client_id
                ))
                return IPV6_EGRESS_DENIED
            else:
                # Unexpected rcode - technical error, not a denial
                print('IPv6 egress: AdGuard rcode={} for {} via ClientID {} - technical error'.format(
                    reply.header.rcode, domain, self.ipv6_egress_client_id
                ))
                return IPV6_EGRESS_ERROR
        except Exception as error:
            # DNS timeout, connection failure, etc. - technical error, not denial
            print('IPv6 egress: AdGuard query failed for {}: {} - technical error'.format(domain, error))
            return IPV6_EGRESS_ERROR

    def is_domain_ipv6_egress(self, domain):
        """Check if a domain is authorized for IPv6 egress.

        Authorization priority depends on ipv6_egress_mode:
          - 'adguard_only':    Only AdGuard Home rules count. File is ignored.
          - 'file_only':       Only file-based list counts. AdGuard is ignored.
          - 'adguard_with_file_fallback':
                               AdGuard is authoritative. File is used ONLY when
                               AdGuard returns a technical error (not denial).

        An explicit SERVFAIL denial from AdGuard is never overridden by the file.
        """
        if not domain:
            return False

        mode = self.ipv6_egress_mode

        # --- File-only mode ---
        if mode == 'file_only':
            return self.is_ipv6_egress_domain(domain)

        # --- AdGuard-involved modes ---
        if self.ipv6_egress_client_id and self.ipv6_egress_enabled:
            result = self.check_ipv6_egress_via_adguard(domain)

            if result == IPV6_EGRESS_AUTHORIZED:
                return True
            if result == IPV6_EGRESS_DENIED:
                # Explicit denial - never fall back to file
                return False
            # IPV6_EGRESS_ERROR: technical failure, may fall back to file
            if mode == 'adguard_only':
                return False
            # mode == 'adguard_with_file_fallback': fall through to file check

        # --- File-based fallback (only for adguard_with_file_fallback or file_only) ---
        if self.ipv6_egress_file and mode in ('adguard_with_file_fallback', 'file_only'):
            if self.is_ipv6_egress_domain(domain):
                return True

        return False

    # ------------------------------------------------------------------
    # IPv6 connectivity check
    # ------------------------------------------------------------------

    def check_ipv6_connectivity(self):
        """Check if the VPS has working IPv6 connectivity.

        Attempts to resolve and connect to a known IPv6 address.
        Results are cached to avoid repeated checks.
        """
        if self.ipv6_connectivity_checked:
            return self.ipv6_connectivity_available

        self.ipv6_connectivity_checked = True

        try:
            # Try to create an IPv6 socket and connect to a known address
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock.settimeout(5)
            # Use Google's public DNS IPv6 address
            sock.connect(('2001:4860:4860::8888', 53))
            sock.close()
            self.ipv6_connectivity_available = True
            print('IPv6 egress: IPv6 connectivity confirmed')
        except (OSError, socket.error) as error:
            self.ipv6_connectivity_available = False
            print('IPv6 egress: IPv6 connectivity NOT available: {}'.format(error))

        return self.ipv6_connectivity_available

    # ------------------------------------------------------------------
    # CNAME chain resolution for IPv6 egress
    # ------------------------------------------------------------------

    def resolve_cname_chain(self, domain, max_hops=10):
        """Follow CNAME chain to find AAAA records for a domain.

        Returns (final_domain, aaaa_addresses) or (domain, []) on failure.
        """
        visited = set()
        current = domain.lower().rstrip('.')

        for hop in range(max_hops):
            if current in visited:
                print('IPv6 egress: CNAME loop detected at {}'.format(current))
                return current, []
            visited.add(current)

            try:
                # Query AAAA for current domain
                aaaa_request = DNSRecord.question(current, 'AAAA')
                reply = self.query_doh(aaaa_request, self.resolver_client_id)

                if reply.header.rcode != RCODE.NOERROR:
                    return current, []

                # Collect AAAA records
                aaaa_records = [
                    str(record.rdata)
                    for record in reply.rr
                    if record.rtype == QTYPE.AAAA
                ]
                if aaaa_records:
                    return current, aaaa_records

                # Check for CNAME
                cname_records = [
                    str(record.rdata).rstrip('.')
                    for record in reply.rr
                    if record.rtype == QTYPE.CNAME
                ]
                if cname_records:
                    current = cname_records[0]
                    continue

                # No AAAA and no CNAME
                return current, []

            except Exception as error:
                print('IPv6 egress: CNAME resolution error for {}: {}'.format(current, error))
                return current, []

        print('IPv6 egress: CNAME chain too long for {}'.format(domain))
        return domain, []

    def add_ipv6_mapping(self, real_ipv6, fake_addr=None):
        """Allocate a virtual IPv4 and create mapping for IPv6 egress.

        Returns the fake IPv4 address or None on failure.
        """
        if fake_addr:
            with self.mapping_lock:
                existing = self.ipv6_mappings.get(real_ipv6)
                if existing:
                    print('IPv6 mapping already exists: {} -> {}'.format(
                        real_ipv6, existing
                    ))
                    return existing
                try:
                    self.unassigned_addresses.remove(fake_addr)
                    self.ipv6_mappings[real_ipv6] = fake_addr
                    self._write_ipv6_mapping_file()
                    print('IPv6 mapping {} -> {}'.format(fake_addr, real_ipv6))
                    return fake_addr
                except ValueError:
                    print('ERROR: Fake addr {} not in unassigned addresses'.format(
                        fake_addr
                    ))
                    return None
        return self.add_ipv6_mappings([real_ipv6]).get(real_ipv6)

    def add_ipv6_mappings(self, real_ipv6_list):
        """Allocate virtual IPv4 addresses for multiple IPv6 destinations.

        Uses a separate reserved address pool so that IPv6 egress pool exhaustion
        cannot destroy regular IPv4 mappings.

        Returns dict: real_ipv6 -> fake_ipv4 (or None on failure).
        """
        unique = list(dict.fromkeys(real_ipv6_list))
        results = {}

        with self.mapping_condition:
            if self.mapping_stopping:
                return {addr: None for addr in unique}

            new_addresses = [
                addr for addr in unique
                if addr not in self.ipv6_mappings
                and addr not in self.ipv6_pending_mappings
            ]

            if len(new_addresses) > len(self.ipv6_unassigned_addresses):
                return {
                    addr: self.ipv6_mappings.get(addr)
                    for addr in unique
                }

            requests = {}
            is_batch_leader = False
            for addr in unique:
                existing_fake = self.ipv6_mappings.get(addr)
                if existing_fake:
                    results[addr] = existing_fake
                    continue
                existing_request = self.ipv6_pending_mappings.get(addr)
                if existing_request:
                    requests[addr] = existing_request
                    continue

                fake_addr = self.ipv6_unassigned_addresses.popleft()
                request = IPv6MappingRequest(addr, fake_addr, threading.Event())
                self.ipv6_pending_mappings[addr] = request
                self.ipv6_mapping_batch.append(request)
                requests[addr] = request

            if requests:
                if not self.ipv6_mapping_batch_running:
                    self.ipv6_mapping_batch_running = True
                    is_batch_leader = True
                self.mapping_condition.notify()

        if is_batch_leader:
            self.apply_ipv6_mapping_batches()

        for request in requests.values():
            request.event.wait()

        return {
            addr: self.ipv6_mappings.get(addr)
            for addr in unique
        }

    def apply_ipv6_mapping_batches(self):
        """Apply pending IPv6 mappings as iptables DNAT rules to the translator."""
        while True:
            with self.mapping_condition:
                deadline = time.monotonic() + self.mapping_batch_delay
                while not self.mapping_stopping:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.mapping_condition.wait(remaining)

                batch = self.ipv6_mapping_batch
                self.ipv6_mapping_batch = []

            rules = ['*nat']
            rules.extend(
                '-A {} -p {} -d {} -j DNAT --to-destination 127.0.0.1:{}'.format(
                    self.ipv6_tablename, protocol, request.fake_addr, self.ipv6_translator_port
                )
                for request in batch
                for protocol in ('tcp', 'udp')
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
                print('ERROR: Failed to execute iptables-restore for IPv6: {}'.format(error))

            success = result is not None and result.returncode == 0
            if result is not None and not success:
                print('ERROR: Failed to add {} IPv6 mappings: {}'.format(
                    len(batch), result.stderr.strip()
                ))

            with self.mapping_condition:
                for request in batch:
                    self.ipv6_pending_mappings.pop(request.real_addr, None)
                    if success:
                        self.ipv6_mappings[request.real_addr] = request.fake_addr
                        self._write_ipv6_mapping_file()
                        print('IPv6 mapping {} -> {}'.format(
                            request.fake_addr, request.real_addr
                        ))
                    else:
                        # Return the fake IPv4 address to the IPv6 egress pool,
                        # not the IPv4 pool, so it remains available for future
                        # IPv6 egress mappings and cannot be allocated to a
                        # regular IPv4 mapping.
                        self.ipv6_unassigned_addresses.appendleft(request.fake_addr)
                    request.event.set()
                if not self.ipv6_mapping_batch:
                    self.ipv6_pending_mappings.clear()
                    self.ipv6_mapping_batch_running = False
                    self.mapping_condition.notify_all()
                    return

    def _write_ipv6_mapping_file(self):
        """Write current IPv6 mappings to the mapping file for the translator."""
        try:
            with open(self.ipv6_mapping_file, 'w', encoding='utf-8') as f:
                for real_ipv6, fake_ipv4 in sorted(self.ipv6_mappings.items()):
                    f.write('{} {}\n'.format(fake_ipv4, real_ipv6))
        except OSError as error:
            print('ERROR: Failed to write IPv6 mapping file: {}'.format(error))

    def get_ipv6_mapping(self, fake_ipv4):
        """Look up real IPv6 by fake IPv4 address."""
        with self.mapping_lock:
            for real_ipv6, fake_addr in self.ipv6_mappings.items():
                if fake_addr == fake_ipv4:
                    return real_ipv6
        return None

    def get_ipv6_mapping_by_real(self, real_ipv6):
        """Look up fake IPv4 by real IPv6 address."""
        with self.mapping_lock:
            return self.ipv6_mappings.get(real_ipv6)

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
            while self.ipv4_mapping_batch_running or self.ipv6_mapping_batch_running:
                self.mapping_condition.wait()
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
        if rule:
            print('ASN match for {}: rule: {}'.format(address, rule))
            return True
        print('ASN no match for {}: AS{}; organization: {}'.format(address, asn, raw_organization))
        return False

    def get_mapping(self, real_addr):
        with self.mapping_lock:
            return self.ipmap.get(real_addr)

    def new_unassigned_addresses(self):
        addresses = deque([str(address) for address in self.iprange.hosts()])
        # Preserve the first address from the range for DNS.
        try:
            addresses.popleft()
        except IndexError as error:
            raise ValueError('IP range has no addresses available for mappings') from error
        if not addresses:
            raise ValueError('IP range has no addresses available for mappings')
        return addresses

    def _reserve_ipv6_addresses(self):
        if not self.ipv6_egress_enabled:
            return
        """Reserve a portion of the virtual IPv4 pool exclusively for IPv6 egress.

        Reserved addresses are moved from the regular unassigned pool to the
        IPv6 egress pool. Regular IPv4 mappings never use these addresses,
        so IPv6 egress pool exhaustion cannot destroy regular IPv4 mappings.
        """
        reserve_count = min(self.ipv6_egress_reserve_count, len(self.unassigned_addresses))
        if reserve_count <= 0:
            return
        for _ in range(reserve_count):
            if self.unassigned_addresses:
                addr = self.unassigned_addresses.popleft()
                self.ipv6_unassigned_addresses.append(addr)
        print('Reserved {} IPv4 addresses for IPv6 egress pool (regular pool: {}, IPv6 pool: {})'.format(
            reserve_count, len(self.unassigned_addresses), len(self.ipv6_unassigned_addresses)
        ))

    def reset_mappings(self):
        """Flush only regular IPv4 mappings. IPv6 egress mappings are preserved."""
        command = ['iptables', '-w', '-t', 'nat', '-F', self.tablename]
        try:
            result = self.iptables_runner(command, capture_output=True, text=True)
        except OSError as error:
            print('ERROR: Failed to flush mappings: {}'.format(error))
            return False
        if result.returncode != 0:
            print('ERROR: Failed to flush mappings: {}'.format(result.stderr.strip()))
            return False

        self.ipmap.clear()
        self.unassigned_addresses = self.new_unassigned_addresses()
        # Re-reserve IPv6 addresses after rebuilding the regular pool
        self._reserve_ipv6_addresses()
        print('Regular IPv4 mappings cleared. IPv6 egress mappings preserved.')
        return True

    def reset_ipv6_mappings(self):
        """Flush only IPv6 egress mappings. Regular IPv4 mappings are preserved."""
        command = ['iptables', '-w', '-t', 'nat', '-F', self.ipv6_tablename]
        try:
            result = self.iptables_runner(command, capture_output=True, text=True)
        except OSError as error:
            print('ERROR: Failed to flush IPv6 egress mappings: {}'.format(error))
            return False
        if result.returncode != 0:
            print('ERROR: Failed to flush IPv6 egress mappings: {}'.format(result.stderr.strip()))
            return False

        self.ipv6_mappings.clear()
        self.ipv6_unassigned_addresses = deque()
        self._reserve_ipv6_addresses()
        print('IPv6 egress mappings cleared. Regular IPv4 mappings preserved.')
        return True

    def add_mapping(self, real_addr, fake_addr=None):
        if fake_addr:
            with self.mapping_lock:
                existing_fake_addr = self.get_mapping(real_addr)
                if existing_fake_addr:
                    print("ERROR: Real addr {} is already mapped to {}, ignoring duplicate mapping to {}".format(
                        real_addr, existing_fake_addr, fake_addr
                    ))
                    return True
                try:
                    self.unassigned_addresses.remove(fake_addr)
                    self.ipmap[real_addr] = fake_addr
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
            if self.mapping_stopping:
                return {real_addr: None for real_addr in unique_addresses}

            new_addresses = [
                real_addr
                for real_addr in unique_addresses
                if not self.get_mapping(real_addr)
                and real_addr not in self.ipv4_pending_mappings
            ]
            if len(new_addresses) > len(self.unassigned_addresses):
                if self.ipv4_pending_mappings:
                    return {real_addr: None for real_addr in unique_addresses}
                if not self.reset_mappings():
                    return {real_addr: None for real_addr in unique_addresses}

            requests = {}
            is_batch_leader = False
            for real_addr in unique_addresses:
                existing_fake_addr = self.get_mapping(real_addr)
                if existing_fake_addr:
                    continue
                existing_request = self.ipv4_pending_mappings.get(real_addr)
                if existing_request:
                    requests[real_addr] = existing_request
                    continue

                fake_addr = self.unassigned_addresses.popleft()
                request = MappingRequest(real_addr, fake_addr, threading.Event())
                self.ipv4_pending_mappings[real_addr] = request
                self.ipv4_mapping_batch.append(request)
                requests[real_addr] = request
            if requests:
                if not self.ipv4_mapping_batch_running:
                    self.ipv4_mapping_batch_running = True
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

                batch = self.ipv4_mapping_batch
                self.ipv4_mapping_batch = []

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
                    self.ipv4_pending_mappings.pop(request.real_addr, None)
                    if success:
                        self.ipmap[request.real_addr] = request.fake_addr
                        print('Mapping {} to {}'.format(request.fake_addr, request.real_addr))
                    else:
                        self.unassigned_addresses.appendleft(request.fake_addr)
                    request.event.set()
                if not self.ipv4_mapping_batch:
                    self.ipv4_pending_mappings.clear()
                    self.ipv4_mapping_batch_running = False
                    self.mapping_condition.notify_all()
                    return

    def resolve(self,request,handler):
        try:
            qname = str(request.q.qname).rstrip('.')
            qtype = request.q.qtype

            # --- IPv6 egress domain handling ---
            # For domains authorized for IPv6 egress, we must never leak real IPv6
            # addresses in any DNS response type. Only A queries receive a virtual
            # IPv4; all other types return empty NOERROR.
            if self.is_domain_ipv6_egress(qname):
                if qtype == QTYPE.A:
                    print('IPv6 egress domain: {} (via AdGuard ClientID={})'.format(
                        qname, self.ipv6_egress_client_id or '(file only)'
                    ))
                    return self._resolve_ipv6_egress(request, qname)
                else:
                    # Block AAAA, HTTPS, SVCB, CNAME, and all other types to prevent
                    # leaking real IPv6 addresses or CNAME targets that could bypass VPN.
                    print('IPv6 egress: blocking QTYPE={} for {} (no real IPv6 leak)'.format(
                        qtype, qname
                    ))
                    empty_reply = request.reply()
                    return empty_reply

            # --- Standard DNS handling (non-IPv6-egress domains) ---

            reply = self.query_doh(request, self.client_id)

            if qtype == QTYPE.AAAA or qtype == QTYPE.HTTPS:
                print('GOT AAAA or HTTPS')
                reply = request.reply()
                return reply

            if qtype == QTYPE.A:
                print('GOT A')

                if reply.header.rcode == RCODE.SERVFAIL:
                    filtered_reply = reply
                    resolved_reply = self.query_doh(request, self.resolver_client_id)
                    if resolved_reply.header.rcode != RCODE.NOERROR:
                        return resolved_reply
                    real_addresses = [
                        str(record.rdata)
                        for record in resolved_reply.rr
                        if record.rtype == QTYPE.A
                    ]
                    if not any(self.is_blocked_asn(address) for address in real_addresses):
                        return filtered_reply
                    reply = resolved_reply

                if not any(record.rtype == QTYPE.A for record in reply.rr):
                    return reply

                newrr = []
                for record in reply.rr:
                    if record.rtype == QTYPE.CNAME:
                        continue
                    newrr.append(record)
                reply.rr = newrr

                real_addresses = [
                    str(record.rdata)
                    for record in reply.rr
                    if record.rtype == QTYPE.A
                ]
                mappings = self.add_mappings(real_addresses)

                for record in reply.rr:
                    if record.rtype != QTYPE.A:
                        continue

                    # print(dir(record))
                    # print(type(record.rdata))

                    real_addr = str(record.rdata)
                    fake_addr = mappings.get(real_addr)
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

    def _resolve_ipv6_egress(self, request, qname):
        """Resolve AAAA for an IPv6 egress domain and return virtual IPv4.

        1. Check IPv6 connectivity on this VPS
        2. Follow CNAME chain to find AAAA records
        3. Select a suitable IPv6 address
        4. Allocate virtual IPv4 from the pool
        5. Create iptables DNAT rule to the translator service
        6. Store mapping (fake_ipv4 -> real_ipv6)
        7. Return A record with virtual IPv4
        """
        try:
            # Check IPv6 connectivity first
            if not self.check_ipv6_connectivity():
                print('IPv6 egress: no IPv6 connectivity on this VPS, rejecting {}'.format(qname))
                err_reply = request.reply()
                err_reply.header.rcode = RCODE.SERVFAIL
                return err_reply

            # Follow CNAME chain to find AAAA records
            resolved_domain, real_ipv6_list = self.resolve_cname_chain(qname)

            if not real_ipv6_list:
                print('IPv6 egress: no AAAA records for {} (resolved to {})'.format(
                    qname, resolved_domain
                ))
                err_reply = request.reply()
                err_reply.header.rcode = RCODE.SERVFAIL
                return err_reply

            # Filter out invalid IPv6 addresses
            valid_ipv6 = []
            for addr in real_ipv6_list:
                try:
                    IPv6Address(addr)
                    valid_ipv6.append(addr)
                except Exception:
                    print('IPv6 egress: skipping invalid IPv6 address: {}'.format(addr))

            if not valid_ipv6:
                print('IPv6 egress: no valid IPv6 addresses for {}'.format(qname))
                err_reply = request.reply()
                err_reply.header.rcode = RCODE.SERVFAIL
                return err_reply

            # Select the first valid IPv6 address
            selected_ipv6 = valid_ipv6[0]
            print('IPv6 egress: selected {} for {} (from {} candidates, CNAME chain: {} -> {})'.format(
                selected_ipv6, qname, len(valid_ipv6), qname, resolved_domain
            ))

            # Check if we already have a mapping for this IPv6
            existing_fake = self.get_ipv6_mapping_by_real(selected_ipv6)
            if existing_fake:
                print('IPv6 egress: reusing existing mapping {} -> {}'.format(
                    existing_fake, selected_ipv6
                ))
                fake_addr = existing_fake
            else:
                # Allocate new virtual IPv4
                mappings = self.add_ipv6_mappings([selected_ipv6])
                fake_addr = mappings.get(selected_ipv6)
                if not fake_addr:
                    print('IPv6 egress: failed to allocate virtual IPv4 for {}'.format(
                        selected_ipv6
                    ))
                    err_reply = request.reply()
                    err_reply.header.rcode = RCODE.SERVFAIL
                    return err_reply

            # Build DNS response with A record containing virtual IPv4
            response = request.reply()
            response.header.rcode = RCODE.NOERROR
            response.add_answer(
                RR(request.q.qname, QTYPE.A, ttl=300, rdata=A(fake_addr))
            )
            print('IPv6 egress: returning A {} -> {} (IPv6: {})'.format(
                qname, fake_addr, selected_ipv6
            ))
            return response

        except Exception as error:
            self.log_processing_error(error)
            err_reply = request.reply()
            err_reply.header.rcode = RCODE.SERVFAIL
            return err_reply


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
                   help="AdGuard client ID used to resolve SERVFAIL responses")
    p.add_argument("--asn-file", default="/root/antizapret/result/asn.txt",
                   help="Semicolon-separated files with blocked ASN numbers and organization names")
    p.add_argument("--asn-database", default="/usr/share/GeoIP/GeoLite2-ASN.mmdb",
                   help="MaxMind ASN database")
    p.add_argument("--ipv6-egress-file", default=None,
                   metavar="<file>",
                   help="File with domains for IPv6 egress (one per line)")
    p.add_argument("--ipv6-translator-port", type=int, default=9000,
                   metavar="<port>",
                   help="Port of the IPv4→IPv6 translator service (default: 9000)")
    p.add_argument("--ipv6-egress-client-id", default=None,
                   metavar="<client-id>",
                   help="AdGuard ClientID for IPv6 egress domain checks (e.g. az-ipv6-local)")
    p.add_argument("--ipv6-egress-enabled", action='store_true', default=False,
                   help="Enable IPv6 egress via AdGuard Home ClientID checks")
    p.add_argument("--ipv6-mapping-file", default=None,
                   metavar="<file>",
                   help="Mapping file for IPv6 egress (default: /root/antizapret/result/ipv6-egress-mappings.txt)")
    p.add_argument("--ipv6-egress-mode", default="adguard_only",
                   metavar="<mode>",
                   help="IPv6 egress authorization mode: adguard_only, file_only, or adguard_with_file_fallback (default: adguard_only)")
    p.add_argument("--ipv6-egress-reserve-count", type=int, default=100,
                   metavar="<count>",
                   help="Number of virtual IPv4 addresses reserved for IPv6 egress pool (default: 100)")
    args = p.parse_args()

    args.dns,_,args.dns_port = args.upstream.partition(':')
    args.dns_port = int(args.dns_port or 53)

    print("Starting Proxy Resolver (%s:%d -> %s:%d) [%s]" % (
          args.address or "*",args.port,
          args.dns,args.dns_port,
          "UDP/TCP" if args.tcp else "UDP"))
    if args.ipv6_egress_file or args.ipv6_egress_enabled:
        print("IPv6 egress enabled: {}".format(args.ipv6_egress_enabled))
        if args.ipv6_egress_file:
            print("IPv6 egress file: {}".format(args.ipv6_egress_file))
        if args.ipv6_egress_client_id:
            print("IPv6 egress AdGuard ClientID: {}".format(args.ipv6_egress_client_id))
        print("IPv6 translator port: {}".format(args.ipv6_translator_port))

    resolver = ProxyResolver(args.dns,args.dns_port,args.doh_port,args.timeout,args.iprange,
                             args.client_id,args.resolver_client_id,
                             args.asn_file,args.asn_database,
                             ipv6_egress_file=args.ipv6_egress_file,
                             ipv6_translator_port=args.ipv6_translator_port,
                             ipv6_egress_client_id=args.ipv6_egress_client_id,
                             ipv6_egress_enabled=args.ipv6_egress_enabled,
                             ipv6_mapping_file=args.ipv6_mapping_file,
                             ipv6_egress_mode=args.ipv6_egress_mode,
                             ipv6_egress_reserve_count=args.ipv6_egress_reserve_count)
    atexit.register(resolver.close)

    def _sighup_handler(signum, frame):
        resolver.request_asn_reload(signum, frame)
        if args.ipv6_egress_file:
            resolver.reload_ipv6_egress_domains(signum, frame)
        # Invalidate IPv6 connectivity cache so it is re-checked on next request
        resolver.ipv6_connectivity_checked = False
        resolver.ipv6_connectivity_available = False

    signal.signal(signal.SIGHUP, _sighup_handler)
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
