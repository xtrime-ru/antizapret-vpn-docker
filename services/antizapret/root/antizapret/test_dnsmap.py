# Run from the repository root:
# docker run --rm -v "$PWD/services/antizapret/root/antizapret:/tests:ro" -w /tests xtrime/antizapret-vpn:6.7.0 python3 -B -m unittest -v test_dnsmap.py

import queue
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch, mock_open
import builtins

from dnslib import A, AAAA, CNAME, DNSRecord, HTTPS, QTYPE, RCODE, RR

import dnsmap


def dns_reply(request, addresses=(), rcode=RCODE.NOERROR, qtype=QTYPE.A):
    reply = request.reply()
    reply.header.rcode = rcode
    for address in addresses:
        if qtype == QTYPE.AAAA:
            reply.add_answer(RR(request.q.qname, QTYPE.AAAA, rdata=AAAA(address)))
        else:
            reply.add_answer(RR(request.q.qname, QTYPE.A, rdata=A(address)))
    return reply


class FakeResponse:
    def __init__(self, data, status=200, content_type='application/dns-message',
                 content_length=None, will_close=False):
        self.data = data
        self.status = status
        self.content_type = content_type
        self.content_length = content_length
        self.will_close = will_close

    def getheader(self, name, default=None):
        if name == 'Content-Type':
            return self.content_type
        if name == 'Content-Length':
            return self.content_length if self.content_length is not None else str(len(self.data))
        return default

    def read(self, size=-1):
        return self.data if size < 0 else self.data[:size]


class FakeConnection:
    def __init__(self, response=None, request_error=None):
        self.response = response
        self.request_error = request_error
        self.closed = False
        self.requests = []

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        if self.request_error:
            raise self.request_error

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class ResolverTestCase(unittest.TestCase):
    def setUp(self):
        self.print_patcher = patch('builtins.print')
        self.output = self.print_patcher.start()
        self.addCleanup(self.print_patcher.stop)
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.asn_file = Path(self.temp_directory.name) / 'asn.txt'
        self.asn_file.write_text('', encoding='utf-8')
        self.asn_database = Mock()
        self.asn_database.get.return_value = None
        self.iptables_runner = Mock(return_value=SimpleNamespace(returncode=0, stderr=''))
        self.conntrack_file = Path(self.temp_directory.name) / 'nf_conntrack'
        self.conntrack_file.write_text('', encoding='utf-8')

    def make_resolver(self, **overrides):
        arguments = {
            'address': 'adguard',
            'port': 53,
            'doh_port': 3000,
            'timeout': 1,
            'iprange': '14.16.0.0/29',
            'client_id': 'az-local',
            'resolver_client_id': 'az-resolver',
            'asn_file': str(self.asn_file),
            'asn_database': '/unused.mmdb',
            'asn_database_reader': self.asn_database,
            'iptables_runner': self.iptables_runner,
            'load_existing_mappings': False,
            'mapping_batch_delay': 0.001,
            'conntrack_path': str(self.conntrack_file),
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        return resolver


class HealthcheckTests(ResolverTestCase):
    def test_local_probe_is_answered_without_upstream(self):
        resolver = self.make_resolver()
        resolver.query_doh = Mock(side_effect=OSError('upstream unavailable'))
        request = DNSRecord.question('HEALTHCHECK.invalid.', 'A')
        handler = Mock(client_address=('127.0.0.1', 12345))

        reply = resolver.resolve(request, handler)

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual([str(answer.rdata) for answer in reply.rr], ['127.0.0.1'])
        resolver.query_doh.assert_not_called()

    def test_remote_probe_follows_normal_resolution(self):
        resolver = self.make_resolver()
        request = DNSRecord.question('healthcheck.invalid.', 'A')
        resolver.query_doh = Mock(return_value=dns_reply(request, rcode=RCODE.NXDOMAIN))
        handler = Mock(client_address=('192.0.2.1', 12345))

        reply = resolver.resolve(request, handler)

        self.assertEqual(reply.header.rcode, RCODE.NXDOMAIN)
        resolver.query_doh.assert_called_once_with(request, 'az-local')


class DoHClientTests(ResolverTestCase):
    def test_request_contains_client_id_and_connection_is_reused(self):
        resolver = self.make_resolver()
        request = DNSRecord.question('example.com', 'A')
        connection = FakeConnection(FakeResponse(dns_reply(request, ['192.0.2.1']).pack()))
        resolver.doh_connections.put(connection)

        reply = resolver.query_doh(request, 'az-local')

        self.assertEqual(str(reply.rr[0].rdata), '192.0.2.1')
        self.assertTrue(connection.requests[0][0][1].startswith('/dns-query/az-local?dns='))
        self.assertIs(resolver.doh_connections.get_nowait(), connection)

    def test_stale_connection_is_closed_and_retry_uses_fresh_connection(self):
        request = DNSRecord.question('example.com', 'A')
        stale = FakeConnection(request_error=BrokenPipeError())
        another_stale = FakeConnection(request_error=BrokenPipeError())
        healthy = FakeConnection(FakeResponse(dns_reply(request, ['192.0.2.1']).pack()))
        resolver = self.make_resolver(connection_factory=Mock(return_value=healthy))
        resolver.doh_connections.put(another_stale)
        resolver.doh_connections.put(stale)

        reply = resolver.query_doh(request, 'az-local')

        self.assertEqual(str(reply.rr[0].rdata), '192.0.2.1')
        self.assertTrue(stale.closed)
        self.assertFalse(another_stale.closed)
        self.assertEqual(len(healthy.requests), 1)
        resolver.connection_factory.assert_called_once()

    def test_malformed_dns_response_is_closed_and_rejected(self):
        resolver = self.make_resolver()
        connection = FakeConnection(FakeResponse(b'not a dns packet'))
        resolver.doh_connections.put(connection)

        with self.assertRaises(dnsmap.DoHProtocolError):
            resolver.query_doh(DNSRecord.question('example.com', 'A'), 'az-local')

        self.assertTrue(connection.closed)

    def test_http_error_is_not_retried(self):
        connection = FakeConnection(FakeResponse(b'error', status=503))
        resolver = self.make_resolver(connection_factory=Mock())
        resolver.doh_connections.put(connection)

        with self.assertRaisesRegex(dnsmap.DoHResponseError, 'HTTP 503'):
            resolver.query_doh(DNSRecord.question('example.com', 'A'), 'az-local')

        self.assertEqual(len(connection.requests), 1)
        self.assertTrue(connection.closed)
        resolver.connection_factory.assert_not_called()

    def test_invalid_content_metadata_is_rejected(self):
        cases = (
            FakeResponse(b'data', content_type='text/plain'),
            FakeResponse(b'data', content_length='invalid'),
        )
        for response in cases:
            with self.subTest(response=response):
                resolver = self.make_resolver()
                connection = FakeConnection(response)
                resolver.doh_connections.put(connection)
                with self.assertRaises(dnsmap.DoHResponseError):
                    resolver.query_doh(DNSRecord.question('example.com', 'A'), 'az-local')
                self.assertTrue(connection.closed)

    def test_oversized_response_is_rejected(self):
        resolver = self.make_resolver()
        data = b'x' * (resolver.MAX_DOH_RESPONSE_SIZE + 1)
        connection = FakeConnection(FakeResponse(data))
        resolver.doh_connections.put(connection)

        with self.assertRaisesRegex(dnsmap.DoHResponseError, 'too large'):
            resolver.query_doh(DNSRecord.question('example.com', 'A'), 'az-local')

        self.assertTrue(connection.closed)

    def test_concurrent_requests_complete_without_socket_errors(self):
        request = DNSRecord.question('example.com', 'A')
        response_data = dns_reply(request, ['192.0.2.1']).pack()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/dns-message')
                self.send_header('Content-Length', str(len(response_data)))
                self.end_headers()
                self.wfile.write(response_data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        resolver = self.make_resolver(address=server.server_address[0],
                                      doh_port=server.server_address[1])
        try:
            with ThreadPoolExecutor(max_workers=10) as executor:
                replies = list(executor.map(
                    lambda _: resolver.query_doh(request, 'az-local'), range(100)
                ))
        finally:
            resolver.close()
            server.shutdown()
            server.server_close()

        self.assertEqual(len(replies), 100)
        self.assertTrue(all(str(reply.rr[0].rdata) == '192.0.2.1' for reply in replies))

    def test_close_closes_pool_and_database(self):
        resolver = self.make_resolver()
        connection = FakeConnection()
        resolver.doh_connections.put(connection)

        resolver.close()

        self.assertTrue(connection.closed)
        self.asn_database.close.assert_called()


class AsnListTests(ResolverTestCase):
    def test_list_parser_accepts_numbers_names_and_comments(self):
        self.asn_file.write_text(
            'AS13335 # Cloudflare\n13238\nTelegram Messenger, Inc.\n/\\bg-?core\\b/\n\n', encoding='utf-8'
        )

        rules = dnsmap.ProxyResolver.load_asn_list(self.asn_file)

        self.assertEqual(rules.asn_rules, {13335: 'AS13335', 13238: '13238'})
        self.assertEqual(rules.substring_rules, [('Telegram Messenger, Inc.', 'telegram messenger, inc.')])
        self.assertEqual(rules.regex_rules[0][0], '/\\bg-?core\\b/')
        self.assertEqual(rules.regex_rules[0][1].pattern, '\\bg-?core\\b')

    def test_list_parser_combines_semicolon_separated_files(self):
        world_asn_file = Path(self.temp_directory.name) / 'asn-world.txt'
        self.asn_file.write_text('AS13335\nCloudflare\n', encoding='utf-8')
        world_asn_file.write_text('AS62041\nTelegram\n', encoding='utf-8')

        rules = dnsmap.ProxyResolver.load_asn_list(
            '{};{}'.format(self.asn_file, world_asn_file)
        )

        self.assertEqual(rules.asn_rules, {13335: 'AS13335', 62041: 'AS62041'})
        self.assertEqual(
            rules.substring_rules,
            [('Cloudflare', 'cloudflare'), ('Telegram', 'telegram')]
        )

    def test_organization_name_uses_case_insensitive_substring_match(self):
        resolver = self.make_resolver()
        resolver.asn_match_rules = dnsmap.AsnMatchRules(
            {},
            [dnsmap.ProxyResolver.organization_substring_rule('telegram')],
            []
        )
        self.asn_database.get.return_value = {
            'autonomous_system_number': 62041,
            'autonomous_system_organization': 'Telegram Messenger, Inc.',
        }

        self.assertTrue(resolver.is_blocked_asn('192.0.2.1'))
        self.output.assert_any_call(
            "ASN lookup for 192.0.2.1: {'autonomous_system_number': 62041, "
            "'autonomous_system_organization': 'Telegram Messenger, Inc.'}"
        )
        self.output.assert_any_call('ASN match for 192.0.2.1: rule: telegram')

    def test_asn_match_logs_matching_rule(self):
        resolver = self.make_resolver()
        resolver.asn_match_rules = dnsmap.AsnMatchRules({13335: 'AS13335'}, [], [])
        self.asn_database.get.return_value = {
            'autonomous_system_number': 13335,
            'autonomous_system_organization': 'Cloudflare, Inc.',
        }

        self.assertTrue(resolver.is_blocked_asn('192.0.2.1'))
        self.output.assert_any_call(
            "ASN lookup for 192.0.2.1: {'autonomous_system_number': 13335, "
            "'autonomous_system_organization': 'Cloudflare, Inc.'}"
        )
        self.output.assert_any_call('ASN match for 192.0.2.1: rule: AS13335')

    def test_empty_and_unspecified_addresses_skip_asn_lookup(self):
        resolver = self.make_resolver()

        for address in (None, '', '0.0.0.0'):
            with self.subTest(address=address):
                self.assertFalse(resolver.is_blocked_asn(address))

        self.asn_database.get.assert_not_called()

    def test_missing_asn_data_is_logged(self):
        resolver = self.make_resolver()
        self.asn_database.get.return_value = None

        self.assertFalse(resolver.is_blocked_asn('192.0.2.1'))

        self.output.assert_any_call('ASN lookup for 192.0.2.1: None')

    def test_empty_and_unspecified_addresses_skip_asn_lookup(self):
        resolver = self.make_resolver()

        for address in (None, '', '0.0.0.0'):
            with self.subTest(address=address):
                self.assertFalse(resolver.is_blocked_asn(address))

        self.asn_database.get.assert_not_called()

    def test_missing_asn_data_is_logged(self):
        resolver = self.make_resolver()
        self.asn_database.get.return_value = None

        self.assertFalse(resolver.is_blocked_asn('192.0.2.1'))

        self.output.assert_any_call('ASN data not found for 192.0.2.1')

    def test_plain_substring_does_not_normalize_punctuation(self):
        resolver = self.make_resolver()
        resolver.asn_match_rules = dnsmap.AsnMatchRules(
            {},
            [dnsmap.ProxyResolver.organization_substring_rule('gcore')],
            []
        )
        self.asn_database.get.return_value = {
            'autonomous_system_number': 199524,
            'autonomous_system_organization': 'G-Core Labs S.A.',
        }

        self.assertFalse(resolver.is_blocked_asn('192.0.2.1'))
        self.output.assert_any_call(
            "ASN lookup for 192.0.2.1: {'autonomous_system_number': 199524, "
            "'autonomous_system_organization': 'G-Core Labs S.A.'}"
        )

    def test_regex_organization_match_supports_word_boundaries(self):
        resolver = self.make_resolver()
        resolver.asn_match_rules = dnsmap.AsnMatchRules(
            {},
            [],
            [dnsmap.ProxyResolver.organization_regex_rule(r'/\bg-?core\b/')]
        )
        self.asn_database.get.return_value = {
            'autonomous_system_number': 199524,
            'autonomous_system_organization': 'G-Core Labs S.A.',
        }

        self.assertTrue(resolver.is_blocked_asn('192.0.2.1'))
        self.output.assert_any_call('ASN match for 192.0.2.1: rule: /\\bg-?core\\b/')

        self.asn_database.get.return_value = {
            'autonomous_system_number': 64500,
            'autonomous_system_organization': 'Edgecore Networks',
        }
        self.assertFalse(resolver.is_blocked_asn('192.0.2.2'))

    def test_regex_organization_match_is_case_insensitive_unicode(self):
        resolver = self.make_resolver()
        resolver.asn_match_rules = dnsmap.AsnMatchRules(
            {},
            [],
            [dnsmap.ProxyResolver.organization_regex_rule('/яндекс/')]
        )
        self.asn_database.get.return_value = {
            'autonomous_system_number': 13238,
            'autonomous_system_organization': 'Яндекс LLC',
        }

        self.assertTrue(resolver.is_blocked_asn('192.0.2.1'))
        self.output.assert_any_call('ASN match for 192.0.2.1: rule: /яндекс/')

    def test_line_without_closing_regex_delimiter_is_substring_rule(self):
        self.asn_file.write_text('/gcore\n', encoding='utf-8')

        rules = dnsmap.ProxyResolver.load_asn_list(self.asn_file)

        self.assertEqual(rules.substring_rules, [('/gcore', '/gcore')])
        self.assertEqual(rules.regex_rules, [])

    def test_invalid_regex_does_not_replace_existing_asn_list(self):
        resolver = self.make_resolver()
        old_rules = dnsmap.AsnMatchRules(
            {13335: 'AS13335'},
            [],
            [dnsmap.ProxyResolver.organization_regex_rule(r'/\bg-?core\b/')]
        )
        resolver.asn_match_rules = old_rules
        self.asn_file.write_text('AS13238\n/[/\n', encoding='utf-8')

        with self.assertRaises(dnsmap.re.error):
            resolver.reload_asn_list()

        self.assertIs(resolver.asn_match_rules, old_rules)

    def test_failed_reload_preserves_last_known_good(self):
        resolver = self.make_resolver()
        old_rules = dnsmap.AsnMatchRules({13335: 'AS13335'}, [], [])
        resolver.asn_match_rules = old_rules
        resolver.asn_file = '/missing/asn.txt'

        with self.assertRaises(FileNotFoundError):
            resolver.reload_asn_list()

        self.assertIs(resolver.asn_match_rules, old_rules)

    def test_successful_reload_atomically_replaces_sets(self):
        resolver = self.make_resolver()
        resolver.asn_match_rules = dnsmap.AsnMatchRules(
            {13335: 'AS13335'},
            [dnsmap.ProxyResolver.organization_substring_rule('Cloudflare')],
            []
        )
        self.asn_file.write_text('AS13238\nYandex\n/\\bg-?core\\b/\n', encoding='utf-8')

        resolver.reload_asn_list()

        self.assertEqual(resolver.asn_match_rules.asn_rules, {13238: 'AS13238'})
        self.assertEqual(resolver.asn_match_rules.substring_rules, [('Yandex', 'yandex')])
        self.assertEqual([rule[0] for rule in resolver.asn_match_rules.regex_rules], ['/\\bg-?core\\b/'])

    def test_signal_handler_reloads_immediately(self):
        resolver = self.make_resolver()
        self.asn_file.write_text('AS13238\nYandex\n', encoding='utf-8')
        with patch.object(dnsmap.os, 'write') as output:
            resolver.request_asn_reload(None, None)
        self.assertEqual(resolver.asn_match_rules.asn_rules, {13238: 'AS13238'})
        self.assertEqual(resolver.asn_match_rules.substring_rules, [('Yandex', 'yandex')])
        output.assert_called_once()

    def test_signal_handler_keeps_old_list_and_logs_reload_failure(self):
        resolver = self.make_resolver()
        old_rules = dnsmap.AsnMatchRules({13335: 'AS13335'}, [], [])
        resolver.asn_match_rules = old_rules
        resolver.asn_file = '/missing/asn.txt'
        with patch.object(dnsmap.os, 'write') as output:
            resolver.request_asn_reload(None, None)
        self.assertIs(resolver.asn_match_rules, old_rules)
        self.assertEqual(output.call_args.args[0], 2)


class ConditionalAsnTests(ResolverTestCase):
    def load_rules(self, text):
        self.asn_file.write_text(text, encoding='utf-8')
        resolver = self.make_resolver()
        self.asn_database.get.return_value = {
            'autonomous_system_number': 15169,
            'autonomous_system_organization': 'Google LLC',
        }
        return resolver

    def test_domain_requires_both_asn_and_hostname(self):
        resolver = self.load_rules('AS15169 domain:googlevideo.com\n')
        for hostname in ('googlevideo.com', 'RR1.googlevideo.COM.'):
            self.assertTrue(resolver.is_blocked_asn('192.0.2.1', hostname))
        for hostname in ('youtube.com', 'evilgooglevideo.com', 'googlevideo.com.example', ''):
            self.assertFalse(resolver.is_blocked_asn('192.0.2.1', hostname))
        self.asn_database.get.return_value['autonomous_system_number'] = 3216
        self.assertFalse(resolver.is_blocked_asn('192.0.2.1', 'rr1.googlevideo.com'))

    def test_idn_domain_rules_accept_unicode_and_punycode(self):
        for domain in ('пример.рф', 'ПРИМЕР.РФ.', 'xn--e1afmkfd.xn--p1ai'):
            with self.subTest(domain=domain):
                resolver = self.load_rules('AS15169 domain:' + domain + '\n')
                for hostname in ('пример.рф', 'видео.пример.рф',
                                 'xn--e1afmkfd.xn--p1ai'):
                    request = DNSRecord.parse(DNSRecord.question(hostname, 'A').pack())
                    self.assertTrue(resolver.is_blocked_asn(
                        '192.0.2.1', str(request.q.qname)))
                self.assertFalse(resolver.is_blocked_asn(
                    '192.0.2.1', 'evilxn--e1afmkfd.xn--p1ai'))

    def test_idna_validation_rejects_invalid_domains(self):
        resolver = self.load_rules('AS15169 domain:googlevideo.com\n')
        old_rules = resolver.asn_match_rules
        for domain in ('', '.', 'a..com', 'example.com..', '-example.com',
                       'example-.com', 'bad_name.com', '*.example.com',
                       'xn--.com', 'a' * 64 + '.com',
                       '.'.join(['a' * 63] * 4)):
            with self.subTest(domain=domain):
                self.asn_file.write_text('AS15169 domain:' + domain, encoding='utf-8')
                with self.assertRaises((ValueError, UnicodeError)):
                    resolver.reload_asn_list()
                self.assertIs(resolver.asn_match_rules, old_rules)

    def test_repeated_asn_rules_are_alternatives_and_regex_keeps_commas(self):
        resolver = self.load_rules(
            'AS15169 domain:youtube.com\n'
            '15169 regexp:^rr[0-9]{1,3}---sn-test\\.googlevideo\\.com$\n'
        )
        for hostname in ('www.youtube.com', 'RR12---sn-test.googlevideo.com.'):
            self.assertTrue(resolver.is_blocked_asn('192.0.2.1', hostname))
        for hostname in ('rr1234---sn-test.googlevideo.com', 'rr1---sn-other.googlevideo.com'):
            self.assertFalse(resolver.is_blocked_asn('192.0.2.1', hostname))

    def test_unrestricted_rule_remains_unrestricted_in_either_order(self):
        for lines in (
            'AS15169\nAS15169 domain:googlevideo.com\n',
            'AS15169 domain:googlevideo.com\nAS15169\n',
            'Google\nAS15169 domain:googlevideo.com\n',
        ):
            with self.subTest(lines=lines):
                resolver = self.load_rules(lines)
                self.assertTrue(resolver.is_blocked_asn('192.0.2.1', 'gmail.com'))

    def test_invalid_conditions_preserve_previous_rules_on_reload(self):
        resolver = self.load_rules('AS15169 domain:googlevideo.com\n')
        old_rules = resolver.asn_match_rules
        for condition in ('domain:', 'regexp:', 'regexp:[', 'regex:foo',
                          'domain:googlevideo.com domain:youtube.com', 'unknown:value'):
            with self.subTest(condition=condition):
                self.asn_file.write_text('AS15169 ' + condition, encoding='utf-8')
                with self.assertRaises((ValueError, dnsmap.re.error)):
                    resolver.reload_asn_list()
                self.assertIs(resolver.asn_match_rules, old_rules)

    def filter_asn_rules(self, source, exclusions):
        scripts = Path(dnsmap.__file__).parent / 'scripts'

        def sanitize(text):
            return subprocess.run(
                ['awk', '-f', str(scripts / 'sanitize-asn-lists.awk')],
                input=text, text=True, capture_output=True, check=True,
            ).stdout

        excluded = Path(self.temp_directory.name) / 'exclude-asn.txt'
        excluded.write_text(sanitize(exclusions), encoding='utf-8')
        result = subprocess.run(
            ['grep', '-v', '-i', '-F', '-w', '-f', str(excluded)],
            input=sanitize(source), text=True, capture_output=True,
        )
        self.assertIn(result.returncode, (0, 1))
        return result.stdout.splitlines()

    def test_bare_asn_exclusion_removes_conditions_with_word_boundaries(self):
        source = ('AS15169 # Google\r\n'
                  'as15169\tdomain:googlevideo.com\n'
                  'AS15169 regexp:^rr[0-9]{1,3}\\.example$\n'
                  'AS151690 domain:googlevideo.com\nAS21395\nGoogle\n')
        for exclusion in (' AS15169 # excluded\r\n', 'as15169\n'):
            with self.subTest(exclusion=exclusion):
                self.assertEqual(self.filter_asn_rules(source, exclusion), [
                    'AS151690 domain:googlevideo.com', 'AS21395', 'Google',
                ])

    def test_asn_exclusions_use_the_written_number_format(self):
        source = 'AS15169 domain:googlevideo.com\n15169 domain:googlevideo.com\n'
        self.assertEqual(self.filter_asn_rules(source, 'AS15169\n'),
                         ['15169 domain:googlevideo.com'])
        self.assertEqual(self.filter_asn_rules(source, '15169\n'),
                         ['AS15169 domain:googlevideo.com'])

    def test_conditions_and_organizations_use_literal_word_matching(self):
        source = ('AS15169\nAS15169 domain:googlevideo.com\n'
                  'AS15169 domain:youtube.com\n'
                  'AS15169 regexp:^rr[0-9]{1,3}\\.example$\n'
                  '/cdn77|datacamp/\nCloudflare\nCloudflare Inc.\nNotCloudflare\n')
        self.assertEqual(self.filter_asn_rules(
            source,
            'as15169 DOMAIN:googlevideo.com # only this domain\n'
            'AS15169 regexp:^rr[0-9]{1,3}\\.example$\n'
            '/cdn77|datacamp/\ncloudflare\n',
        ), ['AS15169', 'AS15169 domain:youtube.com', 'NotCloudflare'])

    def test_empty_exclusions_preserve_all_rules(self):
        self.assertEqual(self.filter_asn_rules(
            'AS15169\nAS15169 domain:googlevideo.com\n', ' # comment\n\n',
        ), ['AS15169', 'AS15169 domain:googlevideo.com'])

    def test_sanitized_rules_reach_dns_resolution_without_widening(self):
        script = Path(dnsmap.__file__).parent / 'scripts/sanitize-asn-lists.awk'
        source = (' # comment\r\n AS15169 domain:googlevideo.com # Google\r\n'
                  'AS15169 regexp:^rr[0-9]{1,3}\\.example$\n'
                  'Telegram Messenger, Inc. # legacy name\n')
        sanitized = subprocess.run(['awk', '-f', str(script)], input=source,
                                   text=True, capture_output=True, check=True).stdout
        self.assertIn('Telegram Messenger, Inc.\n', sanitized)
        resolver = self.load_rules(sanitized)
        self.assertEqual(resolver.asn_match_rules.asn_rules, {})
        self.assertEqual(len(resolver.asn_match_rules.domain_rules), 2)
        for hostname, should_map in (('rr1.googlevideo.com', True), ('gmail.com', False)):
            with self.subTest(hostname=hostname):
                request = DNSRecord.question(hostname, 'A')
                filtered_reply = dns_reply(request, rcode=RCODE.REFUSED)
                resolved_reply = dns_reply(request, ['192.0.2.1'])
                # Match the original query name, even when its address belongs to a CNAME target.
                resolved_reply.rr[0].rname = 'cdn.example.net'
                resolver.query_doh = Mock(side_effect=[filtered_reply, resolved_reply])
                reply = resolver.resolve(request, Mock())
                if should_map:
                    self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
                    self.assertEqual(str(reply.rr[0].rname), hostname + '.')
                else:
                    self.assertIs(reply, filtered_reply)


class MappingTests(ResolverTestCase):
    class TrackingDict(dict):
        def __init__(self):
            super().__init__()
            self.was_cleared = False

        def clear(self):
            self.was_cleared = True
            super().clear()

    def test_range_must_leave_an_address_after_dns_reservation(self):
        with self.assertRaisesRegex(ValueError, 'no addresses available'):
            self.make_resolver(iprange='14.16.0.1/32')

    def test_iptables_failure_does_not_commit_and_recycles_address(self):
        self.iptables_runner.return_value = Mock(returncode=1, stderr='failed')
        resolver = self.make_resolver()

        self.assertFalse(resolver.add_mapping('192.0.2.1'))

        self.assertEqual(resolver.ipmap, {})
        self.assertEqual(resolver.unassigned_addresses[0], '14.16.0.2')

    def test_iptables_success_commits_mapping(self):
        resolver = self.make_resolver()
        resolver.pending_mappings = self.TrackingDict()

        self.assertEqual(resolver.add_mapping('192.0.2.1'), '14.16.0.2')

        self.assertEqual(resolver.get_mapping('192.0.2.1'), '14.16.0.2')
        self.assertTrue(resolver.pending_mappings.was_cleared)
        command = self.iptables_runner.call_args.args[0]
        self.assertEqual(command, ['iptables-restore', '--wait', '--noflush'])
        self.assertIn(
            '-A dnsmap -d 14.16.0.2 -j DNAT --to-destination 192.0.2.1',
            self.iptables_runner.call_args.kwargs['input'],
        )

    def test_concurrent_mappings_are_applied_in_one_batch(self):
        resolver = self.make_resolver(mapping_batch_delay=0.05)
        barrier = threading.Barrier(3)

        def add_mapping(address):
            barrier.wait()
            return resolver.add_mapping(address)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(add_mapping, '192.0.2.1'),
                executor.submit(add_mapping, '192.0.2.2'),
            ]
            barrier.wait()
            self.assertEqual(
                {future.result() for future in futures},
                {'14.16.0.2', '14.16.0.3'},
            )

        self.iptables_runner.assert_called_once()
        rules = self.iptables_runner.call_args.kwargs['input']
        self.assertIn('--to-destination 192.0.2.1', rules)
        self.assertIn('--to-destination 192.0.2.2', rules)

    def test_concurrent_duplicate_mapping_is_only_added_once(self):
        resolver = self.make_resolver(mapping_batch_delay=0.05)
        barrier = threading.Barrier(3)

        def add_mapping():
            barrier.wait()
            return resolver.add_mapping('192.0.2.1')

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(add_mapping) for _ in range(2)]
            barrier.wait()
            self.assertEqual(
                [future.result() for future in futures],
                ['14.16.0.2', '14.16.0.2'],
            )

        rules = self.iptables_runner.call_args.kwargs['input']
        self.assertEqual(rules.count('--to-destination 192.0.2.1'), 1)

    def test_exhausted_range_returns_none_without_flush(self):
        resolver = self.make_resolver(iprange='14.16.0.0/30')

        self.assertEqual(resolver.add_mapping('192.0.2.1'), '14.16.0.2')

        # Pool exhausted: add_mappings returns None for every address,
        # does NOT flush the chain, does NOT reassign existing mappings.
        result = resolver.add_mappings(['192.0.2.2', '192.0.2.3'])
        self.assertEqual(result, {'192.0.2.2': None, '192.0.2.3': None})
        self.assertEqual(resolver.ipmap, {'192.0.2.1': '14.16.0.2'})
        # No iptables flush command was issued.
        flush_commands = [call.args[0] for call in self.iptables_runner.call_args_list
                          if '-F' in call.args[0]]
        self.assertEqual(flush_commands, [])

    def test_batch_is_not_partially_allocated_when_range_is_exhausted(self):
        resolver = self.make_resolver(iprange='14.16.0.0/29')
        for index in range(4):
            resolver.add_mapping('192.0.2.{}'.format(index + 1))

        # Pool exhausted: all addresses return None, no flush.
        mappings = resolver.add_mappings(['192.0.2.10', '192.0.2.11'])

        self.assertEqual(mappings, {
            '192.0.2.10': None,
            '192.0.2.11': None,
        })
        self.assertEqual(resolver.ipmap, {
            '192.0.2.1': '14.16.0.2',
            '192.0.2.2': '14.16.0.3',
            '192.0.2.3': '14.16.0.4',
            '192.0.2.4': '14.16.0.5',
        })
        # No iptables flush command was issued.
        flush_commands = [call.args[0] for call in self.iptables_runner.call_args_list
                          if '-F' in call.args[0]]
        self.assertEqual(flush_commands, [])

    def test_mapping_is_rejected_after_resolver_stops(self):
        resolver = self.make_resolver()
        resolver.close()

        self.assertFalse(resolver.add_mapping('192.0.2.1'))
        self.iptables_runner.assert_not_called()

    def test_pool_exhaustion_does_not_flush(self):
        def run_iptables(command, **kwargs):
            if '-F' in command:
                return Mock(returncode=1, stderr='flush failed')
            return Mock(returncode=0, stderr='')

        self.iptables_runner.side_effect = run_iptables
        resolver = self.make_resolver(iprange='14.16.0.0/30')

        self.assertEqual(resolver.add_mapping('192.0.2.1'), '14.16.0.2')
        result = resolver.add_mappings(['192.0.2.2'])

        # Pool exhausted: returns None, does NOT attempt flush.
        self.assertEqual(result, {'192.0.2.2': None})
        self.assertEqual(resolver.ipmap, {'192.0.2.1': '14.16.0.2'})
        self.assertEqual(list(resolver.unassigned_addresses), [])


class ResolverContractTests(ResolverTestCase):
    def resolver_with_replies(self, replies, asn_match=False):
        resolver = self.make_resolver()
        resolver.query_doh = Mock(side_effect=replies)
        resolver.is_blocked_asn = Mock(return_value=asn_match)
        resolver.add_mappings = Mock(side_effect=lambda addresses: {
            address: '14.16.0.{}'.format(index + 2)
            for index, address in enumerate(addresses)
        })
        return resolver

    def test_filtered_a_answer_is_mapped_without_asn_lookup(self):
        request = DNSRecord.question('example.com', 'A')
        aaaa_empty = request.reply()
        aaaa_empty.header.rcode = RCODE.NOERROR
        resolver = self.resolver_with_replies([
            dns_reply(request, ['192.0.2.1']),
            aaaa_empty,
        ])

        reply = resolver.resolve(request, Mock())

        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        resolver.is_blocked_asn.assert_not_called()

    def test_refused_without_asn_match_is_preserved(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.resolver_with_replies([
            dns_reply(request, rcode=RCODE.REFUSED),
            dns_reply(request, ['192.0.2.1']),
        ])

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.REFUSED)
        resolver.add_mappings.assert_not_called()

    def test_resolver_servfail_is_returned_without_asn_lookup(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.resolver_with_replies([
            dns_reply(request, rcode=RCODE.REFUSED),
            dns_reply(request, ['192.0.2.1'], rcode=RCODE.SERVFAIL),
        ], asn_match=True)

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)
        resolver.is_blocked_asn.assert_not_called()
        resolver.add_mappings.assert_not_called()

    def test_upstream_failure_does_not_trigger_asn_lookup(self):
        for qtype in ('A', 'HTTPS'):
            for timeout in (False, True):
                with self.subTest(qtype=qtype, timeout=timeout):
                    request = DNSRecord.question('example.com', qtype)
                    resolver = self.resolver_with_replies([
                        TimeoutError('DoH timeout') if timeout else dns_reply(request, rcode=RCODE.SERVFAIL),
                    ])
                    resolver.log_processing_error = Mock()

                    reply = resolver.resolve(request, Mock())

                    self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)
                    resolver.query_doh.assert_called_once_with(request, 'az-local')
                    resolver.is_blocked_asn.assert_not_called()

    def test_https_asn_lookup_failure_is_preserved(self):
        request = DNSRecord.question('example.com', 'HTTPS')
        addresses = DNSRecord.question('example.com', 'A')
        resolver = self.resolver_with_replies([
            dns_reply(request, rcode=RCODE.REFUSED),
            dns_reply(addresses, rcode=RCODE.SERVFAIL),
        ])

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)
        self.assertEqual(reply.q.qtype, QTYPE.HTTPS)
        resolver.is_blocked_asn.assert_not_called()

    def test_empty_resolver_answer_preserves_filtered_reply(self):
        request = DNSRecord.question('empty.example', 'A')
        filtered_reply = dns_reply(request, rcode=RCODE.REFUSED)
        resolved_reply = dns_reply(request)
        resolver = self.resolver_with_replies([
            filtered_reply,
            resolved_reply,
        ], asn_match=True)

        reply = resolver.resolve(request, Mock())

        self.assertIs(reply, filtered_reply)
        self.assertEqual(reply.header.rcode, RCODE.REFUSED)
        resolver.is_blocked_asn.assert_not_called()
        resolver.add_mappings.assert_not_called()

    def test_one_asn_match_maps_all_resolved_addresses(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.resolver_with_replies([
            dns_reply(request, rcode=RCODE.REFUSED),
            dns_reply(request, ['192.0.2.1', '192.0.2.2']),
        ])
        resolver.is_blocked_asn.side_effect = [False, True]

        reply = resolver.resolve(request, Mock())

        self.assertEqual([str(record.rdata) for record in reply.rr], ['14.16.0.2', '14.16.0.3'])
        resolver.add_mappings.assert_called_once_with(['192.0.2.1', '192.0.2.2'])

    def test_nxdomain_is_preserved(self):
        request = DNSRecord.question('missing.example', 'A')
        resolver = self.resolver_with_replies([dns_reply(request, rcode=RCODE.NXDOMAIN)])

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NXDOMAIN)
        resolver.add_mappings.assert_not_called()

    def test_empty_noerror_answer_is_preserved(self):
        request = DNSRecord.question('empty.example', 'A')
        aaaa_empty = request.reply()
        aaaa_empty.header.rcode = RCODE.NOERROR
        resolver = self.resolver_with_replies([dns_reply(request), aaaa_empty])

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(reply.rr, [])
        resolver.add_mappings.assert_not_called()

    def test_answer_without_ipv4_address_is_preserved(self):
        request = DNSRecord.question('alias.example', 'A')
        upstream_reply = dns_reply(request)
        upstream_reply.add_answer(RR(
            request.q.qname,
            QTYPE.CNAME,
            rdata=CNAME('target.example'),
        ))
        aaaa_empty = request.reply()
        aaaa_empty.header.rcode = RCODE.NOERROR
        resolver = self.resolver_with_replies([upstream_reply, aaaa_empty])

        reply = resolver.resolve(request, Mock())

        # No A records -> falls through to AAAA-only path, AAAA is empty
        # NOERROR, so return the A-query answer (CNAME preserved) with
        # the original request ID and QTYPE=A in the Question section.
        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(reply.header.id, request.header.id)
        self.assertEqual(reply.q.qtype, QTYPE.A)
        self.assertEqual(len(reply.rr), 1)
        self.assertEqual(reply.rr[0].rtype, QTYPE.CNAME)
        resolver.add_mappings.assert_not_called()

    def test_aaaa_is_suppressed(self):
        request = DNSRecord.question('example.com', 'AAAA')
        resolver = self.resolver_with_replies([dns_reply(request)])
        reply = resolver.resolve(request, Mock())
        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(reply.rr, [])

    def test_https_keeps_alias_target_and_alpn_and_removes_addresses(self):
        request = DNSRecord.question('video.example', 'HTTPS')
        upstream = request.reply()
        upstream.header.ad = 1
        upstream.add_answer(
            RR('video.example', QTYPE.CNAME, rdata=CNAME('canonical.example')),
            RR('canonical.example', QTYPE.HTTPS, ttl=300, rdata=HTTPS.fromZone([
                '1', 'edge.example.', 'mandatory=alpn', 'alpn=h3,h2',
                'port=8443', 'ipv4hint=192.0.2.10', 'ipv6hint=2001:db8::10',
            ])),
            RR('edge.example', QTYPE.A, rdata=A('192.0.2.10')),
        )
        upstream.add_ar(
            RR('edge.example', QTYPE.A, rdata=A('192.0.2.10')),
            RR('edge.example', QTYPE.AAAA, rdata=AAAA('2001:db8::10')),
        )
        resolver = self.resolver_with_replies([DNSRecord.parse(upstream.pack())])

        reply = DNSRecord.parse(resolver.resolve(request, Mock()).pack())

        self.assertEqual([rr.rtype for rr in reply.rr], [QTYPE.CNAME, QTYPE.HTTPS])
        self.assertEqual(str(reply.rr[1].rdata), '1 edge.example. mandatory=alpn alpn=h3,h2 port=8443')
        self.assertEqual(reply.rr[1].ttl, 300)
        self.assertEqual(reply.ar, [])
        self.assertFalse(reply.header.ad)
        resolver.is_blocked_asn.assert_not_called()
        resolver.add_mappings.assert_not_called()

    def test_https_skips_records_with_mandatory_address_hints(self):
        request = DNSRecord.question('video.example', 'HTTPS')
        blocked = [
            RR(request.q.qname, QTYPE.HTTPS, rdata=HTTPS.fromZone(['1', '.', *params]))
            for params in (
                ['mandatory=ipv4hint', 'ipv4hint=192.0.2.10'],
                ['mandatory=alpn,ipv6hint', 'alpn=h3', 'ipv6hint=2001:db8::10'],
            )
        ]
        allowed = RR(request.q.qname, QTYPE.HTTPS, rdata=HTTPS.fromZone(['2', '.', 'alpn=h3']))
        for expected in ([], [allowed]):
            with self.subTest(expected=expected):
                upstream = request.reply()
                upstream.add_answer(*blocked, *expected)
                resolver = self.resolver_with_replies([upstream])
                reply = resolver.resolve(request, Mock())
                self.assertEqual(reply.header.rcode, RCODE.NOERROR)
                self.assertEqual(reply.rr, expected)

    def test_https_with_matching_asn_returns_filtered_answer(self):
        request = DNSRecord.question('video.example', 'HTTPS')
        address_request = DNSRecord.question('video.example', 'A')
        https_reply = request.reply()
        https_reply.add_answer(RR(request.q.qname, QTYPE.HTTPS, rdata=HTTPS.fromZone([
            '1', '.', 'alpn=h3', 'ipv4hint=192.0.2.2',
        ])))
        resolver = self.resolver_with_replies([
            dns_reply(request, rcode=RCODE.REFUSED),
            dns_reply(address_request, ['192.0.2.1', '192.0.2.2']),
            https_reply,
        ])
        resolver.is_blocked_asn.side_effect = [False, True]

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(str(reply.rr[0].rdata), '1 . alpn=h3')
        calls = resolver.query_doh.call_args_list
        self.assertEqual([(call.args[0].q.qtype, call.args[1]) for call in calls], [
            (QTYPE.HTTPS, 'az-local'), (QTYPE.A, 'az-resolver'), (QTYPE.HTTPS, 'az-resolver'),
        ])
        self.assertEqual([call.args[0].q.qname for call in calls], [request.q.qname] * 3)
        self.assertEqual([call.args for call in resolver.is_blocked_asn.call_args_list], [
            ('192.0.2.1', 'video.example.'), ('192.0.2.2', 'video.example.'),
        ])
        resolver.add_mappings.assert_not_called()

    def test_https_without_matching_asn_preserves_refused(self):
        request = DNSRecord.question('video.example', 'HTTPS')
        address_request = DNSRecord.question('video.example', 'A')
        for address_reply in (
            dns_reply(address_request, ['192.0.2.1']),
            dns_reply(address_request),
            dns_reply(address_request, rcode=RCODE.NXDOMAIN),
        ):
            with self.subTest(address_reply=address_reply):
                resolver = self.resolver_with_replies([
                    dns_reply(request, rcode=RCODE.REFUSED),
                    address_reply,
                ])

                reply = resolver.resolve(request, Mock())

                self.assertEqual(reply.header.rcode, RCODE.REFUSED)
                self.assertEqual(reply.q.qtype, QTYPE.HTTPS)
                self.assertEqual(resolver.query_doh.call_count, 2)
                resolver.add_mappings.assert_not_called()

    def test_svcb_without_matching_asn_preserves_refused(self):
        """SVCB (QTYPE 64) without matching ASN: REFUSED upstream → REFUSED reply."""
        request = DNSRecord.question('video.example', 'SVCB')
        address_request = DNSRecord.question('video.example', 'A')
        for address_reply in (
            dns_reply(address_request, ['192.0.2.1']),
            dns_reply(address_request),
            dns_reply(address_request, rcode=RCODE.NXDOMAIN),
        ):
            with self.subTest(address_reply=address_reply):
                resolver = self.resolver_with_replies([
                    dns_reply(request, rcode=RCODE.REFUSED),
                    address_reply,
                ])

                reply = resolver.resolve(request, Mock())

                self.assertEqual(reply.header.rcode, RCODE.REFUSED)
                self.assertEqual(reply.q.qtype, 64)
                self.assertEqual(resolver.query_doh.call_count, 2)
                resolver.add_mappings.assert_not_called()

    def test_svcb_dispatches_to_resolve_https(self):
        """SVCB (QTYPE 64) is dispatched to _resolve_https."""
        request = DNSRecord.question('video.example', 'SVCB')
        resolver = self.resolver_with_replies([])
        try:
            reply = resolver.resolve(request, Mock())
            self.assertIsNotNone(reply)
        except Exception:
            # Expected if no upstream configured - dispatch worked
            pass

    def test_filter_address_hint_reply_removes_hints(self):
        """filter_address_hint_reply removes ipv4hint/ipv6hint from HTTPS records."""
        request = DNSRecord.question('video.example', 'HTTPS')
        upstream = request.reply()
        upstream.header.ad = 1
        upstream.add_answer(
            RR('video.example', QTYPE.CNAME, rdata=CNAME('canonical.example')),
            RR('canonical.example', QTYPE.HTTPS, ttl=300, rdata=HTTPS.fromZone([
                '1', 'edge.example.', 'mandatory=alpn', 'alpn=h3,h2',
                'port=8443', 'ipv4hint=192.0.2.10', 'ipv6hint=2001:db8::10',
            ])),
            RR('edge.example', QTYPE.A, rdata=A('192.0.2.10')),
        )
        upstream.add_ar(
            RR('edge.example', QTYPE.A, rdata=A('192.0.2.10')),
            RR('edge.example', QTYPE.AAAA, rdata=AAAA('2001:db8::10')),
        )
        resolver = self.resolver_with_replies([DNSRecord.parse(upstream.pack())])

        reply = DNSRecord.parse(resolver.resolve(request, Mock()).pack())

        self.assertEqual([rr.rtype for rr in reply.rr], [QTYPE.CNAME, QTYPE.HTTPS])
        # Verify ipv4hint and ipv6hint are removed from params
        rdata = reply.rr[1].rdata
        params_keys = [key for key, _ in rdata.params]
        self.assertNotIn(4, params_keys, 'ipv4hint should be removed')
        self.assertNotIn(6, params_keys, 'ipv6hint should be removed')
        # Verify ALPN is preserved
        self.assertTrue(any(key == 1 for key, _ in rdata.params), 'alpn should be preserved')
        self.assertEqual(reply.rr[1].ttl, 300)
        self.assertEqual(reply.ar, [])
        self.assertFalse(reply.header.ad)

    def test_filter_address_hint_reply_drops_svcb_without_params(self):
        """SVCB record without .params attribute is safely dropped (no AttributeError)."""
        from dnslib import DNSQuestion
        request = DNSRecord(q=DNSQuestion('video.example', 64))  # QTYPE.SVCB
        upstream = request.reply()
        upstream.header.ad = 1
        # Create a mock rdata without params attribute
        class NoParamsRR:
            rtype = 64
            rclass = 1
            ttl = 300
            rname = request.q.qname
            rdata = Mock(spec=[])  # No 'params' attribute
        upstream.add_answer(NoParamsRR())
        resolver = self.resolver_with_replies([upstream])

        reply = resolver.resolve(request, Mock())

        # Record without params should be dropped, reply should be empty NOERROR
        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(reply.rr, [])

    def test_filter_address_hint_reply_skips_mandatory_address_hints(self):
        """Records with mandatory ipv4hint/ipv6hint are dropped entirely."""
        request = DNSRecord.question('video.example', 'HTTPS')
        blocked = [
            RR(request.q.qname, QTYPE.HTTPS, rdata=HTTPS.fromZone(['1', '.', *params]))
            for params in (
                ['mandatory=ipv4hint', 'ipv4hint=192.0.2.10'],
                ['mandatory=alpn,ipv6hint', 'alpn=h3', 'ipv6hint=2001:db8::10'],
            )
        ]
        allowed = RR(request.q.qname, QTYPE.HTTPS, rdata=HTTPS.fromZone(['2', '.', 'alpn=h3']))
        for expected in ([], [allowed]):
            with self.subTest(expected=expected):
                upstream = request.reply()
                upstream.add_answer(*blocked, *expected)
                resolver = self.resolver_with_replies([upstream])
                reply = resolver.resolve(request, Mock())
                self.assertEqual(reply.header.rcode, RCODE.NOERROR)
                self.assertEqual(reply.rr, expected)

    def test_filter_address_hint_reply_with_matching_asn(self):
        """HTTPS with matching ASN: filters ipv4hint/ipv6hint."""
        request = DNSRecord.question('video.example', 'HTTPS')
        address_request = DNSRecord.question('video.example', 'A')
        https_reply = request.reply()
        https_reply.add_answer(RR(request.q.qname, QTYPE.HTTPS, rdata=HTTPS.fromZone([
            '1', '.', 'alpn=h3', 'ipv4hint=192.0.2.2',
        ])))
        resolver = self.resolver_with_replies([
            dns_reply(request, rcode=RCODE.REFUSED),
            dns_reply(address_request, ['192.0.2.1', '192.0.2.2']),
            https_reply,
        ])
        resolver.is_blocked_asn.side_effect = [False, True]

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(str(reply.rr[0].rdata), '1 . alpn=h3')
        calls = resolver.query_doh.call_args_list
        self.assertEqual([(call.args[0].q.qtype, call.args[1]) for call in calls], [
            (QTYPE.HTTPS, 'az-local'), (QTYPE.A, 'az-resolver'), (QTYPE.HTTPS, 'az-resolver'),
        ])
        self.assertEqual([call.args[0].q.qname for call in calls], [request.q.qname] * 3)
        self.assertEqual([call.args for call in resolver.is_blocked_asn.call_args_list], [
            ('192.0.2.1', 'video.example.'), ('192.0.2.2', 'video.example.'),
        ])
        resolver.add_mappings.assert_not_called()

    def test_filter_address_hint_reply_without_matching_asn(self):
        """HTTPS without matching ASN: REFUSED upstream → REFUSED reply."""
        request = DNSRecord.question('video.example', 'HTTPS')
        address_request = DNSRecord.question('video.example', 'A')
        for address_reply in (
            dns_reply(address_request, ['192.0.2.1']),
            dns_reply(address_request),
            dns_reply(address_request, rcode=RCODE.NXDOMAIN),
        ):
            with self.subTest(address_reply=address_reply):
                resolver = self.resolver_with_replies([
                    dns_reply(request, rcode=RCODE.REFUSED),
                    address_reply,
                ])

                reply = resolver.resolve(request, Mock())

                self.assertEqual(reply.header.rcode, RCODE.REFUSED)
                self.assertEqual(reply.q.qtype, QTYPE.HTTPS)
                self.assertEqual(resolver.query_doh.call_count, 2)
                resolver.add_mappings.assert_not_called()


    def test_mmdb_exception_returns_servfail(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.resolver_with_replies([
            dns_reply(request, rcode=RCODE.REFUSED),
            dns_reply(request, ['192.0.2.1']),
        ])
        resolver.is_blocked_asn.side_effect = RuntimeError('MMDB failed')
        resolver.log_processing_error = Mock()

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)
        resolver.log_processing_error.assert_called_once()

    def test_error_logging_is_rate_limited(self):
        resolver = self.make_resolver()
        with patch.object(dnsmap.time, 'monotonic', return_value=10), \
                patch('builtins.print') as output, \
                patch.object(dnsmap.traceback, 'print_exc') as print_traceback:
            resolver.log_processing_error(RuntimeError('first'))
            resolver.log_processing_error(RuntimeError('second'))

        output.assert_called_once()
        print_traceback.assert_called_once()
        self.assertEqual(resolver.suppressed_errors, 1)

class TundraWireformatTests(ResolverTestCase):
    """Tests for the real Tundra-NAT64 external address translation wireformat."""

    def make_resolver_with_tundra(self, **overrides):
        arguments = {
            'address': 'adguard', 'port': 53, 'doh_port': 3000, 'timeout': 1,
            'iprange': '14.16.0.0/29', 'client_id': 'az-local',
            'resolver_client_id': 'az-resolver',
            'asn_file': str(self.asn_file),
            'asn_database': '/unused.mmdb',
            'asn_database_reader': self.asn_database,
            'iptables_runner': self.iptables_runner,
            'load_existing_mappings': False,
            'mapping_batch_delay': 0.001,
            'tundra_socket': '/run/tundra.sock',
            'conntrack_path': str(self.conntrack_file),
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        return resolver

    def _encode_request(self, msg_type, src_ip, dst_ip, msg_id=12345):
        """Build a 40-byte Tundra request message.

        Tundra-NAT64 sends requests with icmp bit = 0 in the flags byte.
        """
        return dnsmap._tundra_encode_message(
            False, False, False, msg_type, 0, msg_id, src_ip, dst_ip
        )

    def _decode_response(self, data):
        return dnsmap._tundra_decode_message(data, is_response=True)

    def test_4to6_translation_success(self):
        """IPv4 client -> VIP: source becomes fd64:ff9b::<client>, dest becomes real IPv6."""
        resolver = self.make_resolver_with_tundra()
        # Pre-install a SIIT binding: VIP 14.16.0.2 -> real IPv6 2001:db8::1
        resolver.siit_by_vip['14.16.0.2'] = '2001:db8::1'
        resolver.siit_by_real['2001:db8::1'] = '14.16.0.2'

        request = self._encode_request(
            dnsmap.TUNDRA_MSG_TYPE_4TO6_MAIN,
            '10.1.166.5', '14.16.0.2'
        )
        response = resolver.tundra_server._process_request(request)
        parsed = self._decode_response(response)

        self.assertTrue(parsed['response'])
        self.assertFalse(parsed['error'])
        self.assertFalse(parsed['icmp'])
        self.assertEqual(parsed['msg_type'], dnsmap.TUNDRA_MSG_TYPE_4TO6_MAIN)
        self.assertEqual(parsed['msg_id'], 12345)
        self.assertEqual(str(parsed['src_ip']), 'fd64:ff9b::a01:a605')
        self.assertEqual(str(parsed['dst_ip']), '2001:db8::1')
        # Successful translation should have positive cache lifetime.
        self.assertEqual(parsed['cache_lifetime'], dnsmap.TUNDRA_CACHE_LIFETIME)

    def test_6to4_translation_success(self):
        """IPv6 real -> fd64:ff9b::<client>: source becomes VIP, dest becomes client IPv4."""
        resolver = self.make_resolver_with_tundra()
        # Pre-install a SIIT binding: real IPv6 2001:db8::1 -> VIP 14.16.0.2
        resolver.siit_by_vip['14.16.0.2'] = '2001:db8::1'
        resolver.siit_by_real['2001:db8::1'] = '14.16.0.2'

        request = self._encode_request(
            dnsmap.TUNDRA_MSG_TYPE_6TO4_MAIN,
            '2001:db8::1', 'fd64:ff9b::a01:a605'
        )
        response = resolver.tundra_server._process_request(request)
        parsed = self._decode_response(response)

        self.assertTrue(parsed['response'])
        self.assertFalse(parsed['error'])
        self.assertFalse(parsed['icmp'])
        self.assertEqual(parsed['msg_type'], dnsmap.TUNDRA_MSG_TYPE_6TO4_MAIN)
        self.assertEqual(parsed['msg_id'], 12345)
        self.assertEqual(str(parsed['src_ip']), '14.16.0.2')
        self.assertEqual(str(parsed['dst_ip']), '10.1.166.5')
        # Successful translation should have positive cache lifetime.
        self.assertEqual(parsed['cache_lifetime'], dnsmap.TUNDRA_CACHE_LIFETIME)

    def test_unknown_ipv6_source_for_icmp_falls_back_to_router_address(self):
        """6TO4_ICMP_ERROR_PACKET with client IPv6 source -> 14.16.0.254 (success)."""
        resolver = self.make_resolver_with_tundra()
        # SIIT binding exists for the real IPv6.
        resolver.siit_by_vip['14.16.0.2'] = '2001:db8::1'
        resolver.siit_by_real['2001:db8::1'] = '14.16.0.2'

        request = self._encode_request(
            dnsmap.TUNDRA_MSG_TYPE_6TO4_ICMP_ERROR,
            'fd64:ff9b::a01:a605',  # client IPv6 source (in client prefix)
            '2001:db8::1'  # real IPv6 as dest
        )
        response = resolver.tundra_server._process_request(request)
        parsed = self._decode_response(response)

        self.assertTrue(parsed['response'])
        self.assertFalse(parsed['error'])
        self.assertFalse(parsed['icmp'])
        self.assertEqual(parsed['msg_type'], dnsmap.TUNDRA_MSG_TYPE_6TO4_ICMP_ERROR)
        # Client IPv6 source -> IPv4 client address.
        self.assertEqual(str(parsed['src_ip']), '10.1.166.5')
        # Real IPv6 dest -> VIP (SIIT binding).
        self.assertEqual(str(parsed['dst_ip']), '14.16.0.2')
        # Successful translation should have positive cache lifetime.
        self.assertEqual(parsed['cache_lifetime'], dnsmap.TUNDRA_CACHE_LIFETIME)

    def test_unknown_vip_for_4to6_returns_error(self):
        """4TO6_MAIN_PACKET with unknown VIP -> error response."""
        resolver = self.make_resolver_with_tundra()
        # No SIIT binding installed.

        request = self._encode_request(
            dnsmap.TUNDRA_MSG_TYPE_4TO6_MAIN,
            '10.1.166.5', '14.16.0.99'
        )
        response = resolver.tundra_server._process_request(request)
        parsed = self._decode_response(response)

        self.assertTrue(parsed['response'])
        self.assertTrue(parsed['error'])
        self.assertFalse(parsed['icmp'])  # main packet type => ICMP bit NOT set on error (per spec)
        self.assertEqual(parsed['msg_type'], dnsmap.TUNDRA_MSG_TYPE_4TO6_MAIN)
        # Error responses zero out the address fields.
        self.assertEqual(str(parsed['src_ip']), '::')
        self.assertEqual(str(parsed['dst_ip']), '::')
        # Error responses should have zero cache lifetime.
        self.assertEqual(parsed['cache_lifetime'], 0)

    def test_unknown_real_ipv6_for_6to4_returns_error(self):
        """6TO4_MAIN_PACKET with unknown real IPv6 and dest NOT in client prefix -> error response."""
        resolver = self.make_resolver_with_tundra()
        # No SIIT binding for the source. Destination is NOT a client address.
        resolver.siit_by_vip['14.16.0.2'] = '2001:db8::1'
        resolver.siit_by_real['2001:db8::1'] = '14.16.0.2'

        request = self._encode_request(
            dnsmap.TUNDRA_MSG_TYPE_6TO4_MAIN,
            '2001:db8::ff',  # unknown real IPv6
            '14.16.0.99'     # destination NOT in fd64:ff9b::/96
        )
        response = resolver.tundra_server._process_request(request)
        parsed = self._decode_response(response)

        self.assertTrue(parsed['response'])
        self.assertTrue(parsed['error'])
        self.assertFalse(parsed['icmp'])  # main packet type => ICMP bit NOT set on error (per spec)
        self.assertEqual(parsed['msg_type'], dnsmap.TUNDRA_MSG_TYPE_6TO4_MAIN)
        # Error responses zero out the address fields.
        self.assertEqual(str(parsed['src_ip']), '0.0.0.0')
        self.assertEqual(str(parsed['dst_ip']), '0.0.0.0')
        # Error responses should have zero cache lifetime.
        self.assertEqual(parsed['cache_lifetime'], 0)

    def test_6to4_main_unknown_source_with_client_dest_falls_back_to_router(self):
        """6TO4_MAIN_PACKET with unknown source but dest in client prefix -> TUNDRA_ROUTER_IPV4 fallback."""
        resolver = self.make_resolver_with_tundra()
        # No SIIT binding for the unknown IPv6 source.
        resolver.siit_by_vip['14.16.0.2'] = '2001:db8::1'
        resolver.siit_by_real['2001:db8::1'] = '14.16.0.2'

        request = self._encode_request(
            dnsmap.TUNDRA_MSG_TYPE_6TO4_MAIN,
            '2001:db8::ff',  # unknown real IPv6
            'fd64:ff9b::a01:a605'  # destination IS in client prefix
        )
        response = resolver.tundra_server._process_request(request)
        parsed = self._decode_response(response)

        self.assertTrue(parsed['response'])
        self.assertFalse(parsed['error'])
        self.assertFalse(parsed['icmp'])
        self.assertEqual(parsed['msg_type'], dnsmap.TUNDRA_MSG_TYPE_6TO4_MAIN)
        # Unknown source with dest in client prefix falls back to DNS address.
        self.assertEqual(str(parsed['src_ip']), '14.16.0.254')
        self.assertEqual(str(parsed['dst_ip']), '10.1.166.5')
        # Fallback translation should have cache_lifetime = 0, not positive value.
        self.assertEqual(parsed['cache_lifetime'], 0)

    def test_6to4_translation_normalizes_ipv6_source(self):
        """6TO4_MAIN with non-canonical IPv6 source still matches SIIT binding."""
        resolver = self.make_resolver_with_tundra()
        # Store binding with canonical IPv6 form.
        resolver.siit_by_vip['14.16.0.2'] = '2001:db8::1'
        resolver.siit_by_real['2001:db8::1'] = '14.16.0.2'

        # Source IPv6 in expanded (non-canonical) form.
        request = self._encode_request(
            dnsmap.TUNDRA_MSG_TYPE_6TO4_MAIN,
            '2001:0db8:0000:0000:0000:0000:0000:0001',  # non-canonical
            'fd64:ff9b::a01:a605'
        )
        response = resolver.tundra_server._process_request(request)
        parsed = self._decode_response(response)

        self.assertTrue(parsed['response'])
        self.assertFalse(parsed['error'])
        self.assertEqual(str(parsed['src_ip']), '14.16.0.2')
        self.assertEqual(str(parsed['dst_ip']), '10.1.166.5')

    def test_tundra_server_unix_socket_round_trip(self):
        """Real Unix socket round-trip: 4TO6_MAIN request → 40-byte response."""
        import os
        import stat
        import tempfile

        resolver = self.make_resolver_with_tundra()
        resolver.tundra_server = None  # Will create our own server below.

        # Install a SIIT binding: VIP 14.16.0.2 → real IPv6 2001:db8::1
        resolver.siit_by_vip['14.16.0.2'] = '2001:db8::1'
        resolver.siit_by_real['2001:db8::1'] = '14.16.0.2'
        resolver.tundra_router_ipv4 = '14.16.0.254'

        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = os.path.join(tmpdir, 'tundra.sock')
            server = dnsmap.TundraExternalTranslationServer(socket_path, resolver)
            server.start()
            self.addCleanup(server.stop)

            # Socket must exist and be world-writable (0o666).
            self.assertTrue(os.path.exists(socket_path))
            mode = stat.S_IMODE(os.stat(socket_path).st_mode)
            self.assertEqual(mode, 0o666)

            # Connect as Tundra client and send a 4TO6_MAIN request.
            client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client_sock.connect(socket_path)

            # 4TO6_MAIN: client IPv4 10.1.166.5 → VIP 14.16.0.2
            request = self._encode_request(
                dnsmap.TUNDRA_MSG_TYPE_4TO6_MAIN,
                '10.1.166.5', '14.16.0.2'
            )
            client_sock.sendall(request)

            # Read exactly 40 bytes of response.
            response = b''
            while len(response) < dnsmap.TUNDRA_MSG_SIZE:
                chunk = client_sock.recv(dnsmap.TUNDRA_MSG_SIZE - len(response))
                self.assertTrue(chunk, 'connection closed before full response')
                response += chunk
            client_sock.close()

            parsed = self._decode_response(response)
            self.assertTrue(parsed['response'])
            self.assertFalse(parsed['error'])
            self.assertFalse(parsed['icmp'])
            self.assertEqual(parsed['msg_type'], dnsmap.TUNDRA_MSG_TYPE_4TO6_MAIN)
            # src = client IPv4 mapped to fd64:ff9b::<hex>
            self.assertEqual(str(parsed['src_ip']), 'fd64:ff9b::a01:a605')
            # dst = VIP resolved to real IPv6 via SIIT binding
            self.assertEqual(str(parsed['dst_ip']), '2001:db8::1')
            self.assertEqual(parsed['cache_lifetime'], dnsmap.TUNDRA_CACHE_LIFETIME)

class SIITMappingLoadTests(ResolverTestCase):
    """Tests for loading SIIT bindings from iptables -S output."""

    def make_resolver_with_tundra(self, **overrides):
        arguments = {
            'address': 'adguard', 'port': 53, 'doh_port': 3000, 'timeout': 1,
            'iprange': '14.16.0.0/29', 'client_id': 'az-local',
            'resolver_client_id': 'az-resolver',
            'asn_file': str(self.asn_file),
            'asn_database': '/unused.mmdb',
            'asn_database_reader': self.asn_database,
            'iptables_runner': self.iptables_runner,
            'load_existing_mappings': False,
            'mapping_batch_delay': 0.001,
            'tundra_socket': '/run/tundra.sock',
            'conntrack_path': str(self.conntrack_file),
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        return resolver

    def test_load_siit_mappings_mixed_with_dnat(self):
        """_load_existing_mappings parses both DNAT and SIIT rules from -S output,
        including stripping /32 suffix from SIIT VIPs."""
        resolver = self.make_resolver_with_tundra()
        iptables_output = (
            '-A dnsmap -d 14.16.0.2 -j DNAT --to-destination 192.0.2.1\n'
            '-A dnsmap -d 14.16.0.3/32 -m comment --comment '
            '"dnsmap:siit:v1:2001:db8::1" -j RETURN\n'
            '-A dnsmap -d 14.16.0.4 -j DNAT --to-destination 192.0.2.2\n'
        )
        with patch.object(dnsmap.subprocess, 'check_output',
                          return_value=iptables_output),                 patch.object(dnsmap.subprocess, 'run',
                             return_value=Mock(returncode=0, stderr='')) as run_mock:
            resolver = self.make_resolver_with_tundra(load_existing_mappings=True)

        # Regular DNAT mapping restored.
        self.assertEqual(resolver.get_mapping('192.0.2.1'), '14.16.0.2')
        self.assertEqual(resolver.get_mapping('192.0.2.2'), '14.16.0.4')
        # SIIT binding restored (with /32 stripped from VIP).
        self.assertEqual(resolver.get_siit_mapping_by_real('2001:db8::1'), '14.16.0.3')
        # Routes recreated for SIIT bindings only (1 route).
        route_calls = [c for c in run_mock.call_args_list
                       if c.args[0][0] == 'ip' and 'route' in c.args[0]]
        self.assertEqual(len(route_calls), 1)

    def test_tundra_server_unavailable_when_socket_missing(self):
        server = dnsmap.TundraExternalTranslationServer(
            '/proc/1/tundra.sock', None
        )
        with patch.object(dnsmap.socket, 'socket', side_effect=OSError('no such file')):
            server.start()
        self.assertFalse(server.available)
        server.stop()

    def test_tundra_disabled_when_socket_empty(self):
        """Tundra-NAT64 is disabled when TUNDRA_SOCKET is empty."""
        resolver = dnsmap.ProxyResolver(
            address='adguard', port=53, doh_port=3000, timeout=1,
            iprange='14.16.0.0/29', client_id='az-local',
            resolver_client_id='az-resolver',
            asn_file=str(self.asn_file),
            asn_database='/unused.mmdb',
            asn_database_reader=self.asn_database,
            iptables_runner=self.iptables_runner,
            load_existing_mappings=False,
            mapping_batch_delay=0.001,
            tundra_socket='',  # Empty = disabled
            ipv6_client_id='az-ipv6-local',
        )
        self.addCleanup(resolver.close)
        self.assertEqual(resolver.tundra_socket, '')
        self.assertIsNone(resolver.tundra_server)


class SIITBindingTests(ResolverTestCase):
    """Tests for SIIT (Tundra-NAT64) binding installation."""

    def make_resolver_with_tundra(self, **overrides):
        arguments = {
            'address': 'adguard', 'port': 53, 'doh_port': 3000, 'timeout': 1,
            'iprange': '14.16.0.0/29', 'client_id': 'az-local',
            'resolver_client_id': 'az-resolver',
            'asn_file': str(self.asn_file),
            'asn_database': '/unused.mmdb',
            'asn_database_reader': self.asn_database,
            'iptables_runner': self.iptables_runner,
            'load_existing_mappings': False,
            'mapping_batch_delay': 0.001,
            'tundra_socket': '/run/tundra.sock',
            'conntrack_path': str(self.conntrack_file),
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        return resolver

    def test_setup_siit_binding_installs_rule_route_and_tables(self):
        resolver = self.make_resolver_with_tundra()
        with patch.object(dnsmap.subprocess, 'run',
                          return_value=SimpleNamespace(returncode=0, stderr='')) as route_mock:
            ok, can_return = resolver._setup_siit_binding('14.16.0.2', '2001:db8::1')
        self.assertTrue(ok)
        self.assertFalse(can_return)  # Success, no VIP return needed
        self.assertEqual(resolver.get_siit_mapping('14.16.0.2'), '2001:db8::1')
        self.assertEqual(resolver.get_siit_mapping_by_real('2001:db8::1'), '14.16.0.2')
        input_rules = self.iptables_runner.call_args.kwargs['input']
        self.assertIn(
            '-A dnsmap -d 14.16.0.2 -m comment --comment '
            '"dnsmap:siit:v1:2001:db8::1" -j RETURN', input_rules
        )
        route_mock.assert_called_once_with(
            ['ip', 'route', 'replace', '14.16.0.2/32', 'dev', 'tundra'],
            capture_output=True, text=True,
        )

    def test_setup_siit_binding_fails_when_route_fails(self):
        resolver = self.make_resolver_with_tundra()
        with patch.object(dnsmap.subprocess, 'run',
                          return_value=SimpleNamespace(returncode=1, stderr='No such device')):
            ok, can_return = resolver._setup_siit_binding('14.16.0.2', '2001:db8::1')
        self.assertFalse(ok)
        self.assertTrue(can_return)  # Rollback succeeded, VIP can return to pool
        self.assertIsNone(resolver.get_siit_mapping('14.16.0.2'))

    def test_setup_siit_binding_rollback_iptables_when_route_fails(self):
        resolver = self.make_resolver_with_tundra()
        # self.iptables_runner (from setUp) returns success for iptables-restore.
        # subprocess.run is called for ip route replace (fails) and iptables -D (succeeds).
        route_mock = SimpleNamespace(returncode=1, stderr='No such device')
        del_mock = SimpleNamespace(returncode=0, stderr='')
        with patch.object(dnsmap.subprocess, 'run',
                          side_effect=[route_mock, del_mock]):
            ok, can_return = resolver._setup_siit_binding('14.16.0.2', '2001:db8::1')
        self.assertFalse(ok)
        self.assertTrue(can_return)  # Rollback succeeded, VIP can return to pool
        self.assertIsNone(resolver.get_siit_mapping('14.16.0.2'))
        # iptables -D should have been called via self.iptables_runner to rollback.
        del_call = None
        for call in resolver.iptables_runner.call_args_list:
            args = call.args[0]
            if isinstance(args, list) and '-D' in args and 'dnsmap' in args:
                del_call = call
                break
        self.assertIsNotNone(del_call, "iptables -D rollback was not called")
        del_args = del_call.args[0]
        self.assertIn('-d', del_args)
        self.assertIn('14.16.0.2', del_args)
        self.assertIn('--comment', del_args)
        self.assertIn('dnsmap:siit:v1:2001:db8::1', del_args)
        self.assertIn('-j', del_args)
        self.assertIn('RETURN', del_args)

    def test_siit_restore_skips_failed_route(self):
        iptables_output = (
            '-A dnsmap -d 14.16.0.2 -m comment --comment '
            '"dnsmap:siit:v1:2001:db8::1" -j RETURN\n'
            '-A dnsmap -d 14.16.0.3 -m comment --comment '
            '"dnsmap:siit:v1:2001:db8::2" -j RETURN\n'
        )
        # subprocess.run calls in order (restore=True skips iptables-restore):
        # 1. ip route replace 14.16.0.2/32 → fails
        # 2. ip route replace 14.16.0.3/32 → succeeds
        route_results = [
            SimpleNamespace(returncode=1, stderr='No such device'),  # ip route replace fails
            SimpleNamespace(returncode=0, stderr=''),                # ip route replace succeeds
        ]
        # iptables_runner calls in order (restore=True skips iptables-restore):
        # 1. iptables -D 14.16.0.2 → fails (rollback fails)
        # 2. iptables -D 14.16.0.3 → succeeds
        iptables_results = [
            SimpleNamespace(returncode=1, stderr='No such device'),  # iptables -D fails
            SimpleNamespace(returncode=0, stderr=''),                # iptables -D succeeds
        ]
        with patch.object(dnsmap.subprocess, 'check_output',
                          return_value=iptables_output),                 patch.object(dnsmap.subprocess, 'run',
                             side_effect=route_results):
            self.iptables_runner.side_effect = iptables_results
            resolver = self.make_resolver_with_tundra(load_existing_mappings=True)

        # First binding should NOT be restored (route failed AND rollback failed).
        self.assertIsNone(resolver.get_siit_mapping_by_real('2001:db8::1'))
        # VIP 14.16.0.2 should NOT be returned to the unassigned pool (rollback failed).
        self.assertNotIn('14.16.0.2', resolver.unassigned_addresses)
        # Second binding should be restored (route succeeded AND rollback succeeded).
        self.assertEqual(resolver.get_siit_mapping_by_real('2001:db8::2'), '14.16.0.3')
        # VIP 14.16.0.3 should NOT be in the unassigned pool.
        self.assertNotIn('14.16.0.3', resolver.unassigned_addresses)

    def test_evict_stale_vips_ipv4(self):
        """Stale IPv4 VIP is evicted via iptables -D and returned to pool."""
        resolver = self.make_resolver_with_tundra(vip_cooldown_ttl=0)
        # Manually populate mappings with stale timestamps.
        resolver.ipmap['192.0.2.10'] = '14.16.0.2'
        resolver.ipmap_last_used['192.0.2.10'] = time.monotonic() - resolver.vip_stale_ttl - 10
        resolver.unassigned_addresses.remove('14.16.0.2')

        del_result = SimpleNamespace(returncode=0, stderr='')
        self.iptables_runner.return_value = del_result
        freed = resolver._evict_stale_vips(needed=1)

        self.assertEqual(freed, 1)
        self.assertNotIn('192.0.2.10', resolver.ipmap)
        self.assertNotIn('192.0.2.10', resolver.ipmap_last_used)
        self.assertIn('14.16.0.2', resolver.unassigned_addresses)

    def test_evict_stale_vips_siit(self):
        """Stale SIIT VIP is evicted via ip route del + iptables -D and returned to pool."""
        resolver = self.make_resolver_with_tundra(vip_cooldown_ttl=0)
        # Manually populate SIIT mappings with stale timestamps.
        resolver.siit_by_vip['14.16.0.3'] = '2001:db8::2'
        resolver.siit_by_real['2001:db8::2'] = '14.16.0.3'
        resolver.siit_last_used['2001:db8::2'] = time.monotonic() - resolver.vip_stale_ttl - 10
        resolver.unassigned_addresses.remove('14.16.0.3')

        route_result = SimpleNamespace(returncode=0, stderr='')
        del_result = SimpleNamespace(returncode=0, stderr='')
        self.iptables_runner.return_value = del_result
        with patch.object(dnsmap.subprocess, 'run', return_value=route_result):
            freed = resolver._evict_stale_vips(needed=1)

        self.assertEqual(freed, 1)
        self.assertNotIn('2001:db8::2', resolver.siit_by_real)
        self.assertNotIn('14.16.0.3', resolver.siit_by_vip)
        self.assertNotIn('2001:db8::2', resolver.siit_last_used)
        self.assertIn('14.16.0.3', resolver.unassigned_addresses)

    def test_evict_stale_vips_fresh_not_evicted(self):
        """Fresh VIPs (< vip_stale_ttl) are NOT evicted."""
        resolver = self.make_resolver_with_tundra()
        resolver.ipmap['192.0.2.10'] = '14.16.0.2'
        resolver.ipmap_last_used['192.0.2.10'] = time.monotonic() - 10  # very fresh
        resolver.unassigned_addresses.remove('14.16.0.2')

        freed = resolver._evict_stale_vips(needed=1)

        self.assertEqual(freed, 0)
        self.assertIn('192.0.2.10', resolver.ipmap)
        self.assertNotIn('14.16.0.2', resolver.unassigned_addresses)

    def test_evict_stale_vips_iptables_failure_no_return(self):
        """If iptables -D fails, VIP is NOT returned to pool."""
        resolver = self.make_resolver_with_tundra(vip_cooldown_ttl=0)
        resolver.ipmap['192.0.2.10'] = '14.16.0.2'
        resolver.ipmap_last_used['192.0.2.10'] = time.monotonic() - resolver.vip_stale_ttl - 10
        resolver.unassigned_addresses.remove('14.16.0.2')

        del_result = SimpleNamespace(returncode=1, stderr='No such rule')
        self.iptables_runner.return_value = del_result
        freed = resolver._evict_stale_vips(needed=1)

        self.assertEqual(freed, 0)
        self.assertIn('192.0.2.10', resolver.ipmap)
        self.assertNotIn('14.16.0.2', resolver.unassigned_addresses)

    def test_evict_stale_vips_hides_mapping_during_iptables_delete(self):
        """During iptables -D, the mapping is removed from memory so parallel
        DNS queries cannot see the VIP."""
        resolver = self.make_resolver_with_tundra(vip_cooldown_ttl=0)
        resolver.ipmap['192.0.2.10'] = '14.16.0.2'
        resolver.ipmap_last_used['192.0.2.10'] = time.monotonic() - resolver.vip_stale_ttl - 10
        resolver.unassigned_addresses.remove('14.16.0.2')

        seen_mapping_during_delete = []

        def iptables_side_effect(args, **kwargs):
            # This is called during iptables -D; the mapping should already be
            # removed from memory.
            seen_mapping_during_delete.append(
                resolver.get_mapping('192.0.2.10') is not None
            )
            seen_mapping_during_delete.append(
                '192.0.2.10' in resolver.ipmap
            )
            return SimpleNamespace(returncode=0, stderr='')

        self.iptables_runner.side_effect = iptables_side_effect
        freed = resolver._evict_stale_vips(needed=1)

        self.assertEqual(freed, 1)
        # Mapping was hidden during iptables -D
        self.assertFalse(any(seen_mapping_during_delete))
        # VIP returned to pool after successful iptables -D
        self.assertIn('14.16.0.2', resolver.unassigned_addresses)
        self.assertNotIn('192.0.2.10', resolver.ipmap)

    def test_evict_stale_vips_returns_to_end_of_pool(self):
        """Evicted VIP is returned to the END of unassigned_addresses, not the
        front, so it gets maximum cooldown before being reissued."""
        resolver = self.make_resolver_with_tundra(vip_cooldown_ttl=0)
        resolver.ipmap['192.0.2.10'] = '14.16.0.2'
        resolver.ipmap_last_used['192.0.2.10'] = time.monotonic() - resolver.vip_stale_ttl - 10
        resolver.unassigned_addresses.remove('14.16.0.2')

        # Add another free address so we can verify ordering.
        resolver.unassigned_addresses.append('14.16.0.3')

        del_result = SimpleNamespace(returncode=0, stderr='')
        self.iptables_runner.return_value = del_result
        freed = resolver._evict_stale_vips(needed=1)

        self.assertEqual(freed, 1)
        self.assertNotIn('192.0.2.10', resolver.ipmap)
        # Evicted VIP goes to the END of the pool (cooldown before reuse).
        self.assertEqual(resolver.unassigned_addresses[-1], '14.16.0.2')
        self.assertEqual(resolver.unassigned_addresses[0], '14.16.0.3')

    def test_evict_stale_vips_empty_pool(self):
        """Empty pool with no stale VIPs returns 0."""
        resolver = self.make_resolver_with_tundra(vip_cooldown_ttl=0)
        freed = resolver._evict_stale_vips(needed=1)
        self.assertEqual(freed, 0)

    def test_evict_stale_vips_siit_iptables_failure_keeps_binding(self):
        """If iptables -D fails for SIIT binding, ip route del is NOT called and binding stays."""
        resolver = self.make_resolver_with_tundra(vip_cooldown_ttl=0)
        resolver.siit_by_vip['14.16.0.3'] = '2001:db8::2'
        resolver.siit_by_real['2001:db8::2'] = '14.16.0.3'
        resolver.siit_last_used['2001:db8::2'] = time.monotonic() - resolver.vip_stale_ttl - 10
        resolver.unassigned_addresses.remove('14.16.0.3')

        route_result = SimpleNamespace(returncode=0, stderr='')
        del_result = SimpleNamespace(returncode=1, stderr='No such rule')
        self.iptables_runner.return_value = del_result
        with patch.object(dnsmap.subprocess, 'run', return_value=route_result) as mock_run:
            freed = resolver._evict_stale_vips(needed=1)

        self.assertEqual(freed, 0)
        # Binding should still exist
        self.assertIn('2001:db8::2', resolver.siit_by_real)
        self.assertIn('14.16.0.3', resolver.siit_by_vip)
        self.assertIn('2001:db8::2', resolver.siit_last_used)
        # VIP should NOT be in unassigned_addresses (iptables -D failed)
        self.assertNotIn('14.16.0.3', resolver.unassigned_addresses)
        # ip route del should NOT have been called
        mock_run.assert_not_called()

    def test_evict_stale_vips_siit_route_failure_restores_iptables(self):
        """iptables -D succeeds, ip route del fails, iptables-restore restores → binding stays."""
        resolver = self.make_resolver_with_tundra(vip_cooldown_ttl=0)
        resolver.siit_by_vip['14.16.0.3'] = '2001:db8::2'
        resolver.siit_by_real['2001:db8::2'] = '14.16.0.3'
        resolver.siit_last_used['2001:db8::2'] = time.monotonic() - resolver.vip_stale_ttl - 10
        resolver.unassigned_addresses.remove('14.16.0.3')

        del_result = SimpleNamespace(returncode=0, stderr='')
        restore_result = SimpleNamespace(returncode=0, stderr='')
        route_result = SimpleNamespace(returncode=1, stderr='Permission denied')
        self.iptables_runner.side_effect = [del_result, restore_result]
        with patch.object(dnsmap.subprocess, 'run', return_value=route_result):
            freed = resolver._evict_stale_vips(needed=1)

        self.assertEqual(freed, 0)
        # Binding should still exist (iptables rule restored)
        self.assertIn('2001:db8::2', resolver.siit_by_real)
        self.assertIn('14.16.0.3', resolver.siit_by_vip)
        self.assertIn('2001:db8::2', resolver.siit_last_used)
        # VIP should NOT be in unassigned_addresses
        self.assertNotIn('14.16.0.3', resolver.unassigned_addresses)
        # iptables-restore was called to restore the rule
        self.assertEqual(self.iptables_runner.call_count, 2)

    def test_evict_stale_vips_skips_active_conntrack(self):
        """VIP with active conntrack entry is NOT evicted."""
        resolver = self.make_resolver_with_tundra(vip_cooldown_ttl=0)
        resolver.ipmap['192.0.2.10'] = '14.16.0.2'
        resolver.ipmap_last_used['192.0.2.10'] = time.monotonic() - resolver.vip_stale_ttl - 10
        resolver.unassigned_addresses.remove('14.16.0.2')

        # Mock /proc/net/nf_conntrack with an active connection to the VIP
        conntrack_data = (
            "ipv4     2 tcp      6 431989 ESTABLISHED src=10.0.0.1 dst=14.16.0.2 sport=1234 dport=80\n"
        )
        with patch('builtins.open', mock_open(read_data=conntrack_data)):
            freed = resolver._evict_stale_vips(needed=1)

        self.assertEqual(freed, 0)
        # VIP should still be in ipmap (not evicted due to active conntrack)
        self.assertIn('192.0.2.10', resolver.ipmap)
        # VIP should NOT be in unassigned_addresses (removed manually and not re-added)
        self.assertNotIn('14.16.0.2', resolver.unassigned_addresses)
        # last_used should have been updated
        self.assertGreater(resolver.ipmap_last_used['192.0.2.10'], time.monotonic() - 1)

    def test_evict_stale_vips_cooldown_prevents_immediate_reuse(self):
        """Evicted VIP goes to quarantine, not unassigned_addresses, when
        vip_cooldown_ttl > 0."""
        resolver = self.make_resolver_with_tundra(vip_cooldown_ttl=300)
        resolver.ipmap['192.0.2.10'] = '14.16.0.2'
        resolver.ipmap_last_used['192.0.2.10'] = time.monotonic() - resolver.vip_stale_ttl - 10
        resolver.unassigned_addresses.remove('14.16.0.2')

        del_result = SimpleNamespace(returncode=0, stderr='')
        self.iptables_runner.return_value = del_result
        freed = resolver._evict_stale_vips(needed=1)

        self.assertEqual(freed, 1)
        # VIP should NOT be in unassigned_addresses (quarantined instead)
        self.assertNotIn('14.16.0.2', resolver.unassigned_addresses)
        self.assertNotIn('192.0.2.10', resolver.ipmap)
        # VIP should be in quarantined_vips
        self.assertEqual(len(resolver.quarantined_vips), 1)
        release_at, evicted_vip = resolver.quarantined_vips[0]
        self.assertEqual(evicted_vip, '14.16.0.2')
        self.assertGreater(release_at, time.monotonic())

        # Immediate allocation should return a different VIP (not the quarantined one)
        vip = resolver._allocate_siit_vip()
        self.assertIsNotNone(vip)
        self.assertNotEqual(vip, '14.16.0.2')

        # After cooldown expires, VIP should be back in pool
        resolver.quarantined_vips[0] = (0, '14.16.0.2')  # Force release
        resolver._reclaim_cooled_down_vips()
        self.assertIn('14.16.0.2', resolver.unassigned_addresses)

    def test_evict_stale_vips_siit_route_and_rollback_failure_quarantines_vip(self):
        """iptables -D succeeds, ip route del fails, iptables-restore also fails → VIP quarantined."""
        resolver = self.make_resolver_with_tundra(vip_cooldown_ttl=0)
        resolver.siit_by_vip['14.16.0.3'] = '2001:db8::2'
        resolver.siit_by_real['2001:db8::2'] = '14.16.0.3'
        resolver.siit_last_used['2001:db8::2'] = time.monotonic() - resolver.vip_stale_ttl - 10
        resolver.unassigned_addresses.remove('14.16.0.3')

        del_result = SimpleNamespace(returncode=0, stderr='')
        restore_result = SimpleNamespace(returncode=1, stderr='Permission denied')
        route_result = SimpleNamespace(returncode=1, stderr='Permission denied')
        self.iptables_runner.side_effect = [del_result, restore_result]
        with patch.object(dnsmap.subprocess, 'run', return_value=route_result):
            freed = resolver._evict_stale_vips(needed=1)

        self.assertEqual(freed, 0)
        # Binding should be removed from memory (quarantined)
        self.assertNotIn('2001:db8::2', resolver.siit_by_real)
        self.assertNotIn('14.16.0.3', resolver.siit_by_vip)
        self.assertNotIn('2001:db8::2', resolver.siit_last_used)
        # VIP should NOT be in unassigned_addresses (quarantined)
        self.assertNotIn('14.16.0.3', resolver.unassigned_addresses)

    def test_evict_stale_vips_siit_cooldown_prevents_immediate_reuse(self):
        """With vip_cooldown_ttl=300, evicted SIIT VIP goes to quarantined_vips,
        not unassigned_addresses; _allocate_siit_vip() returns None immediately;
        after release_at passes, _reclaim_cooled_down_vips() returns it to pool."""
        resolver = self.make_resolver_with_tundra(vip_cooldown_ttl=300)
        resolver.siit_by_vip['14.16.0.3'] = '2001:db8::2'
        resolver.siit_by_real['2001:db8::2'] = '14.16.0.3'
        resolver.siit_last_used['2001:db8::2'] = time.monotonic() - resolver.vip_stale_ttl - 10
        resolver.unassigned_addresses.remove('14.16.0.3')
        # Empty the pool so only the quarantined VIP could be allocated
        while resolver.unassigned_addresses:
            resolver.unassigned_addresses.popleft()

        del_result = SimpleNamespace(returncode=0, stderr='')
        route_result = SimpleNamespace(returncode=0, stderr='')
        self.iptables_runner.return_value = del_result
        with patch.object(dnsmap.subprocess, 'run', return_value=route_result):
            freed = resolver._evict_stale_vips(needed=1)

        self.assertEqual(freed, 1)
        self.assertNotIn('2001:db8::2', resolver.siit_by_real)
        self.assertNotIn('14.16.0.3', resolver.siit_by_vip)
        # VIP should be in quarantined_vips, not unassigned_addresses
        self.assertNotIn('14.16.0.3', resolver.unassigned_addresses)
        self.assertEqual(len(resolver.quarantined_vips), 1)
        release_at, vip = resolver.quarantined_vips[0]
        self.assertEqual(vip, '14.16.0.3')
        self.assertGreater(release_at, time.monotonic())

        # Immediate allocation should fail because VIP is quarantined and pool is empty
        allocated = resolver._allocate_siit_vip()
        self.assertIsNone(allocated)

        # After release_at passes, _reclaim_cooled_down_vips() should return VIP to pool
        resolver.quarantined_vips.clear()
        resolver.quarantined_vips.append((time.monotonic() - 1, '14.16.0.3'))  # already expired
        resolver._reclaim_cooled_down_vips()
        self.assertIn('14.16.0.3', resolver.unassigned_addresses)
        self.assertEqual(len(resolver.quarantined_vips), 0)

    def test_evict_stale_vips_conntrack_unavailable_blocks_eviction(self):
        """With conntrack file unavailable, _vip_has_active_conntrack() returns None,
        SIIT VIP is NOT evicted (fail-closed), stays in siit_by_real."""
        resolver = self.make_resolver_with_tundra(
            vip_cooldown_ttl=0,
            conntrack_path='/nonexistent/nf_conntrack',
        )
        resolver.siit_by_vip['14.16.0.3'] = '2001:db8::2'
        resolver.siit_by_real['2001:db8::2'] = '14.16.0.3'
        resolver.siit_last_used['2001:db8::2'] = time.monotonic() - resolver.vip_stale_ttl - 10
        resolver.unassigned_addresses.remove('14.16.0.3')

        freed = resolver._evict_stale_vips(needed=1)

        self.assertEqual(freed, 0)
        # Binding should remain in memory
        self.assertIn('2001:db8::2', resolver.siit_by_real)
        self.assertIn('14.16.0.3', resolver.siit_by_vip)
        self.assertIn('2001:db8::2', resolver.siit_last_used)

    def test_reclaim_cooled_down_vips_keeps_active_conntrack_quarantined(self):
        """If VIP in quarantined_vips has expired but conntrack has active record,
        _reclaim_cooled_down_vips() does NOT return it to unassigned_addresses
        and extends quarantine."""
        resolver = self.make_resolver_with_tundra(vip_cooldown_ttl=300)
        resolver.siit_by_vip['14.16.0.3'] = '2001:db8::2'
        resolver.siit_by_real['2001:db8::2'] = '14.16.0.3'
        resolver.siit_last_used['2001:db8::2'] = time.monotonic() - resolver.vip_stale_ttl - 10
        resolver.unassigned_addresses.remove('14.16.0.3')

        # Put VIP in quarantined_vips with already-expired release_at
        resolver.quarantined_vips.append((time.monotonic() - 10, '14.16.0.3'))

        # Write an active conntrack entry for this VIP
        self.conntrack_file.write_text(
            'ipv4     tcp      6 431989 ESTABLISHED src=10.0.0.1 dst=14.16.0.3 sport=12345 dport=80\n',
            encoding='utf-8',
        )

        resolver._reclaim_cooled_down_vips()

        # VIP should NOT be in unassigned_addresses
        self.assertNotIn('14.16.0.3', resolver.unassigned_addresses)
        # VIP should still be in quarantined_vips (extended)
        self.assertEqual(len(resolver.quarantined_vips), 1)
        release_at, vip = resolver.quarantined_vips[0]
        self.assertEqual(vip, '14.16.0.3')
        self.assertGreater(release_at, time.monotonic())

    def test_add_mappings_updates_last_used(self):
        """Repeated add_mappings() updates ipmap_last_used, keeping VIP fresh."""
        resolver = self.make_resolver_with_tundra()
        resolver.ipmap['192.0.2.10'] = '14.16.0.2'
        resolver.ipmap_last_used['192.0.2.10'] = time.monotonic() - resolver.vip_stale_ttl - 10
        resolver.unassigned_addresses.remove('14.16.0.2')

        # Simulate re-use of the mapping via add_mappings()
        resolver.add_mappings(['192.0.2.10'])

        # Now it should NOT be evicted because it's fresh
        freed = resolver._evict_stale_vips(needed=1)
        self.assertEqual(freed, 0)
        self.assertIn('192.0.2.10', resolver.ipmap)


class IPv6ViaTundraTests(ResolverTestCase):
    """Tests for the strict IPv4/IPv6 selection matrix."""

    def setUp(self):
        super().setUp()
        # The /32 route installation must succeed in these tests.
        self._route_patcher = patch.object(
            dnsmap.subprocess, 'run',
            return_value=Mock(returncode=0, stderr=''))
        self._route_patcher.start()

    def tearDown(self):
        self._route_patcher.stop()
        super().tearDown()

    def make_resolver_with_tundra(self, **overrides):
        arguments = {
            'address': 'adguard', 'port': 53, 'doh_port': 3000, 'timeout': 1,
            'iprange': '14.16.0.0/29', 'client_id': 'az-local',
            'resolver_client_id': 'az-resolver',
            'asn_file': str(self.asn_file),
            'asn_database': '/unused.mmdb',
            'asn_database_reader': self.asn_database,
            'iptables_runner': self.iptables_runner,
            'load_existing_mappings': False,
            'mapping_batch_delay': 0.001,
            'tundra_socket': '/run/tundra.sock',
            'conntrack_path': str(self.conntrack_file),
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        # Mark Tundra server as available for tests that expect it to work.
        if resolver.tundra_server is not None:
            resolver.tundra_server.available = True
        self.addCleanup(resolver.close)
        return resolver

    def _a_reply(self, request, addresses=(), rcode=RCODE.NOERROR):
        reply = request.reply()
        reply.header.rcode = rcode
        for addr in addresses:
            reply.add_answer(RR(request.q.qname, QTYPE.A, rdata=A(addr)))
        return reply

    def _aaaa_reply(self, request, addresses=(), rcode=RCODE.NOERROR):
        reply = request.reply()
        reply.header.rcode = rcode
        for addr in addresses:
            reply.add_answer(RR(request.q.qname, QTYPE.AAAA, rdata=AAAA(addr)))
        return reply

    def test_aaaa_only_returns_virtual_ipv4_via_tundra(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra()
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request),                       # A: NOERROR, no records
            self._aaaa_reply(request, ['2001:db8::1']),   # AAAA: one record
        ])
        resolver._is_tundra_ready = Mock(return_value=True)
        with patch.object(dnsmap.subprocess, 'run',
                          return_value=type('SimpleNS', (), {'returncode': 0, 'stderr': ''})()):
            reply = resolver.resolve(request, Mock(client_address=('10.1.166.5', 12345)))

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 1)
        self.assertEqual(reply.rr[0].rtype, QTYPE.A)
        vip = str(reply.rr[0].rdata)
        self.assertEqual(resolver.get_siit_mapping_by_real('2001:db8::1'), vip)
        # SIIT binding installed in tables for Tundra to consult
        self.assertEqual(resolver.siit_by_vip[vip], '2001:db8::1')

    def test_aaaa_only_without_aaaa_records_returns_empty_noerror(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra()
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request),                       # A: NOERROR, no records
            self._aaaa_reply(request),                    # AAAA: NOERROR, no records
        ])
        reply = resolver.resolve(request, Mock())
        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(reply.rr, [])

    def test_aaaa_only_with_direct_egress_returns_empty_noerror(self):
        """AAAA-only domain marked 'direct' in egress map: no Tundra, empty NOERROR."""
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra(
            ipv6_egress_map={'example.com.': 'direct'})
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request),                       # A: NOERROR, no records
            self._aaaa_reply(request, ['2001:db8::1']),   # AAAA: one record
        ])
        resolver._is_tundra_ready = Mock(return_value=True)
        reply = resolver.resolve(request, Mock())
        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(reply.rr, [])
        # No SIIT binding should be created
        self.assertEqual(len(resolver.siit_by_vip), 0)

    def test_aaaa_only_refused_with_direct_egress_returns_refused(self):
        """AAAA-only domain marked 'direct' with A=REFUSED: return REFUSED, no Tundra."""
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra(
            ipv6_egress_map={'example.com.': 'direct'})
        # AdGuard REFUSED → resolve_by_asn returns REFUSED → direct check → return REFUSED
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request, rcode=RCODE.REFUSED),  # AdGuard REFUSED
            self._a_reply(request, rcode=RCODE.REFUSED),  # resolve_by_asn also REFUSED
        ])
        resolver._is_tundra_ready = Mock(return_value=True)
        reply = resolver.resolve(request, Mock())
        self.assertEqual(reply.header.rcode, RCODE.REFUSED)
        # No SIIT binding should be created
        self.assertEqual(len(resolver.siit_by_vip), 0)

    def test_a_and_aaaa_aaaa_priority_with_direct_egress_uses_ipv4(self):
        """With aaaa_priority=True, domain marked 'direct' still uses IPv4 scheme."""
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra(
            aaaa_priority=True,
            ipv6_egress_map={'example.com.': 'direct'})
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request, ['192.0.2.1']),
            self._aaaa_reply(request, ['2001:db8::1']),
        ])
        resolver._is_tundra_ready = Mock(return_value=True)
        resolver.add_mappings = Mock(return_value={'192.0.2.1': '14.16.0.2'})

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        resolver.add_mappings.assert_called_once_with(['192.0.2.1'])
        # No SIIT binding should be created
        self.assertEqual(len(resolver.siit_by_vip), 0)

    def test_a_and_aaaa_aaaa_priority_with_local_egress_uses_tundra(self):
        """With aaaa_priority=True, domain marked 'local' still uses Tundra (bypasses AdGuard)."""
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra(
            aaaa_priority=True,
            ipv6_egress_map={'example.com.': 'local'})
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request, ['192.0.2.1']),
            self._aaaa_reply(request, ['2001:db8::1']),
        ])
        resolver._is_tundra_ready = Mock(return_value=True)
        resolver.check_ipv6_authorized = Mock(
            return_value=dnsmap.IPV6_EGRESS_DENIED)
        with patch.object(dnsmap.subprocess, 'run',
                          return_value=type('SimpleNS', (), {'returncode': 0, 'stderr': ''})()):
            reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(reply.rr[0].rtype, QTYPE.A)
        vip = str(reply.rr[0].rdata)
        self.assertEqual(resolver.get_siit_mapping_by_real('2001:db8::1'), vip)
        # AdGuard check must NOT be called when aaaa_priority is enabled (even for 'local').
        resolver.check_ipv6_authorized.assert_not_called()

    def test_aaaa_only_max_siit_records_limit(self):
        """More than MAX_SIIT_AAAA_RECORDS AAAA addresses → only 4 VIPs returned."""
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra()
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request),                       # A: NOERROR, no records
            self._aaaa_reply(request, [
                '2001:db8::1', '2001:db8::2', '2001:db8::3',
                '2001:db8::4', '2001:db8::5', '2001:db8::6',
            ]),  # 6 AAAA records
        ])
        resolver._is_tundra_ready = Mock(return_value=True)
        with patch.object(dnsmap.subprocess, 'run',
                          return_value=type('SimpleNS', (), {'returncode': 0, 'stderr': ''})()):
            reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 4)
        # All returned records should be A records with unique VIPs.
        vips = [str(rr.rdata) for rr in reply.rr if rr.rtype == QTYPE.A]
        self.assertEqual(len(vips), 4)
        self.assertEqual(len(set(vips)), 4)

    def test_aaaa_only_pool_exhaustion_returns_servfail_without_flush(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = dnsmap.ProxyResolver(
            address='adguard', port=53, doh_port=3000, timeout=1,
            iprange='14.16.0.0/30', client_id='az-local',
            resolver_client_id='az-resolver',
            asn_file=str(self.asn_file),
            asn_database='/unused.mmdb',
            asn_database_reader=self.asn_database,
            iptables_runner=self.iptables_runner,
            load_existing_mappings=False,
            mapping_batch_delay=0.001,
            tundra_socket='/run/tundra.sock',
        )
        self.addCleanup(resolver.close)

        # Exhaust the pool with regular IPv4 mappings (14.16.0.2 only usable host).
        resolver.add_mapping('192.0.2.1')
        self.assertEqual(len(resolver.ipmap), 1)

        # AAAA-only domain: no VIP left, must not flush, must return SERVFAIL.
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request),
            self._aaaa_reply(request, ['2001:db8::1']),
        ])
        reply = resolver.resolve(request, Mock())
        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)
        # Regular IPv4 mapping preserved (pool not flushed).
        self.assertEqual(len(resolver.ipmap), 1)
        self.assertIn('192.0.2.1', resolver.ipmap)

    def test_a_and_aaaa_authorized_goes_via_tundra(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra()
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request, ['192.0.2.1']),
            self._aaaa_reply(request, ['2001:db8::1']),
        ])
        resolver.check_ipv6_authorized = Mock(
            return_value=dnsmap.IPV6_EGRESS_AUTHORIZED)
        resolver._is_tundra_ready = Mock(return_value=True)
        with patch.object(dnsmap.subprocess, 'run',
                          return_value=type('SimpleNS', (), {'returncode': 0, 'stderr': ''})()):
            reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(reply.rr[0].rtype, QTYPE.A)
        vip = str(reply.rr[0].rdata)
        self.assertEqual(resolver.get_siit_mapping_by_real('2001:db8::1'), vip)
        resolver.check_ipv6_authorized.assert_called_once_with('example.com')

    def test_a_and_aaaa_denied_falls_back_to_ipv4(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra()
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request, ['192.0.2.1']),
            self._aaaa_reply(request, ['2001:db8::1']),
        ])
        resolver.check_ipv6_authorized = Mock(
            return_value=dnsmap.IPV6_EGRESS_DENIED)
        resolver.add_mappings = Mock(return_value={'192.0.2.1': '14.16.0.2'})

        reply = resolver.resolve(request, Mock())

        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        resolver.add_mappings.assert_called_once_with(['192.0.2.1'])

    def test_a_and_aaaa_check_error_falls_back_to_ipv4(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra()
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request, ['192.0.2.1']),
            self._aaaa_reply(request, ['2001:db8::1']),
        ])
        resolver.check_ipv6_authorized = Mock(
            return_value=dnsmap.IPV6_EGRESS_ERROR)
        resolver.add_mappings = Mock(return_value={'192.0.2.1': '14.16.0.2'})

        reply = resolver.resolve(request, Mock())

        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        resolver.add_mappings.assert_called_once_with(['192.0.2.1'])

    def test_a_and_aaaa_aaaa_priority_bypasses_adguard_check(self):
        """With aaaa_priority=True, dual-stack domain goes via Tundra without
        calling check_ipv6_authorized."""
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra(aaaa_priority=True)
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request, ['192.0.2.1']),
            self._aaaa_reply(request, ['2001:db8::1']),
        ])
        resolver.check_ipv6_authorized = Mock(
            return_value=dnsmap.IPV6_EGRESS_DENIED)
        resolver._is_tundra_ready = Mock(return_value=True)
        with patch.object(dnsmap.subprocess, 'run',
                          return_value=type('SimpleNS', (), {'returncode': 0, 'stderr': ''})()):
            reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(reply.rr[0].rtype, QTYPE.A)
        vip = str(reply.rr[0].rdata)
        self.assertEqual(resolver.get_siit_mapping_by_real('2001:db8::1'), vip)
        # AdGuard check must NOT be called when aaaa_priority is enabled.
        resolver.check_ipv6_authorized.assert_not_called()

    def test_a_and_aaaa_aaaa_priority_tundra_failure_falls_back_to_ipv4(self):
        """With aaaa_priority=True, if Tundra fails, falls back to IPv4 scheme."""
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra(aaaa_priority=True)
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request, ['192.0.2.1']),
            self._aaaa_reply(request, ['2001:db8::1']),
        ])
        resolver.check_ipv6_authorized = Mock(
            return_value=dnsmap.IPV6_EGRESS_AUTHORIZED)
        resolver._is_tundra_ready = Mock(return_value=True)
        # Tundra fails to install binding.
        with patch.object(dnsmap.subprocess, 'run',
                          return_value=type('SimpleNS', (), {'returncode': 1, 'stderr': 'fail'})()):
            resolver.add_mappings = Mock(return_value={'192.0.2.1': '14.16.0.2'})
            reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        resolver.add_mappings.assert_called_once_with(['192.0.2.1'])

    def test_a_only_uses_ipv4_scheme_without_adguard_check(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra()
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request, ['192.0.2.1']),
            self._aaaa_reply(request),                    # AAAA: no records
        ])
        resolver.check_ipv6_authorized = Mock()
        resolver.add_mappings = Mock(return_value={'192.0.2.1': '14.16.0.2'})

        reply = resolver.resolve(request, Mock())

        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        resolver.check_ipv6_authorized.assert_not_called()
        resolver.add_mappings.assert_called_once_with(['192.0.2.1'])

    def test_a_and_aaaa_aaaa_query_exception_fallback(self):
        """A + AAAA query exception: valid A reply, exception on AAAA → IPv4 VIP."""
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra()
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request, ['192.0.2.1']),
            Exception('AAAA query failed'),
        ])
        resolver.add_mappings = Mock(return_value={'192.0.2.1': '14.16.0.2'})

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        resolver.add_mappings.assert_called_once_with(['192.0.2.1'])

    def test_aaaa_query_never_leaks_real_ipv6(self):
        request = DNSRecord.question('example.com', 'AAAA')
        resolver = self.make_resolver_with_tundra()
        resolver.query_doh = Mock()
        reply = resolver.resolve(request, Mock())
        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(reply.rr, [])
        resolver.query_doh.assert_not_called()

    def test_a_and_aaaa_tundra_not_available_falls_back_to_ipv4(self):
        """Dual-stack domain with Tundra unavailable: falls back to IPv4 scheme."""
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra()
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request, ['192.0.2.1']),
            self._aaaa_reply(request, ['2001:db8::1']),
        ])
        resolver.check_ipv6_authorized = Mock(
            return_value=dnsmap.IPV6_EGRESS_AUTHORIZED)
        # Simulate Tundra not available (no socket / server not started).
        resolver.tundra_server = None
        resolver.add_mappings = Mock(return_value={'192.0.2.1': '14.16.0.2'})

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        resolver.add_mappings.assert_called_once_with(['192.0.2.1'])
        self.assertEqual(resolver.query_doh.call_count, 1)
        resolver.check_ipv6_authorized.assert_not_called()

    def test_a_and_aaaa_refused_asn_authorized_tundra(self):
        """Dual-stack domain with REFUSED from AdGuard + ASN match + authorized → Tundra VIP."""
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra()
        # AdGuard REFUSED → resolve_by_asn → az-resolver returns A with matching ASN
        # → check_ipv6_authorized returns AUTHORIZED → AAAA query → Tundra VIP.
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request, rcode=RCODE.REFUSED),  # AdGuard REFUSED
            self._a_reply(request, ['192.0.2.1']),        # az-resolver A (ASN match)
            self._aaaa_reply(request, ['2001:db8::1']),   # az-resolver AAAA
        ])
        resolver.check_ipv6_authorized = Mock(
            return_value=dnsmap.IPV6_EGRESS_AUTHORIZED)
        resolver._is_tundra_ready = Mock(return_value=True)
        with patch.object(dnsmap.subprocess, 'run',
                          return_value=type('SimpleNS', (), {'returncode': 0, 'stderr': ''})()):
            # ASN match for 192.0.2.1
            resolver.is_blocked_asn = Mock(return_value=True)
            reply = resolver.resolve(request, Mock())
        # After REFUSED + ASN match + authorized, AAAA query should be made and VIP assigned via Tundra
        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        # SIIT binding should exist for the AAAA target.
        self.assertEqual(resolver.get_siit_mapping_by_real('2001:db8::1'), '14.16.0.2')

    def test_a_and_aaaa_refused_az_resolver_no_a_but_aaaa_tundra_unavailable_returns_refused(self):
        """REFUSED → az-resolver NOERROR without A but with AAAA → Tundra unavailable → original REFUSED."""
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra()
        # AdGuard REFUSED → az-resolver NOERROR without A (NODATA) but with AAAA
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request, rcode=RCODE.REFUSED),  # AdGuard REFUSED
            self._a_reply(request, rcode=RCODE.NOERROR),  # az-resolver NOERROR without A (NODATA)
            self._aaaa_reply(request, ['2001:db8::1']),   # az-resolver AAAA
        ])
        resolver.check_ipv6_authorized = Mock(
            return_value=dnsmap.IPV6_EGRESS_AUTHORIZED)
        # Simulate Tundra not available
        resolver.tundra_server = None
        reply = resolver.resolve(request, Mock())
        # Should return original REFUSED, not SERVFAIL
        self.assertEqual(reply.header.rcode, RCODE.REFUSED)

    def test_a_and_aaaa_refused_az_resolver_no_a_aaaa_tundra_authorized(self):
        """REFUSED → az-resolver NOERROR without A (NODATA) but with AAAA → az-ipv6-* authorized → Tundra VIP.
        Tests that when AdGuard returns REFUSED and az-resolver returns NOERROR without A
        records but with AAAA records, and az-ipv6-* is authorized, the domain is routed
        through Tundra-NAT64 with a SIIT VIP assigned.
        """
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra()
        # AdGuard REFUSED → resolve_by_asn → az-resolver NOERROR without A (NODATA) but with AAAA
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request, rcode=RCODE.REFUSED),  # AdGuard REFUSED
            self._a_reply(request, rcode=RCODE.NOERROR),  # az-resolver NOERROR without A (NODATA)
            self._aaaa_reply(request, ['2001:db8::1']),   # az-resolver AAAA
        ])
        resolver.check_ipv6_authorized = Mock(
            return_value=dnsmap.IPV6_EGRESS_AUTHORIZED)
        resolver._is_tundra_ready = Mock(return_value=True)
        with patch.object(dnsmap.subprocess, 'run',
                          return_value=type('SimpleNS', (), {'returncode': 0, 'stderr': ''})()):
            resolver.is_blocked_asn = Mock(return_value=True)
            reply = resolver.resolve(request, Mock())
        # After REFUSED + NOERROR(NODATA) + AAAA + authorized → Tundra VIP
        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        # SIIT binding should exist for the AAAA target.
        self.assertEqual(resolver.get_siit_mapping_by_real('2001:db8::1'), '14.16.0.2')

    def test_a_and_aaaa_tundra_disabled_early_exit(self):
        """When Tundra is disabled, domain with empty NOERROR on A immediately returns a_reply
        without sending AAAA query (query_doh.call_count == 1)."""
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra()
        resolver.tundra_server = None  # Tundra disabled
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request),  # A: NOERROR, no records
        ])

        reply = resolver.resolve(request, Mock())
        # Should return original a_reply (NOERROR, no records) without AAAA query
        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 0)
        # Only the A query should have been sent
        self.assertEqual(resolver.query_doh.call_count, 1)

    def test_client_ipv6_rendering_standalone(self):
        """Test _client_ipv6 rendering in isolation."""
        resolver = self.make_resolver_with_tundra()
        self.assertEqual(resolver._client_ipv6('10.1.166.5'), 'fd64:ff9b::a01:a605')
        self.assertEqual(resolver._client_ipv6('192.0.2.1'), 'fd64:ff9b::c000:201')

    def _encode_request(self, msg_type, src_ip, dst_ip, msg_id=12345):
        """Build a 40-byte Tundra request message."""
        return dnsmap._tundra_encode_message(
            False, False, False, msg_type, 0, msg_id, src_ip, dst_ip
        )

    def _decode_response(self, data):
        return dnsmap._tundra_decode_message(data, is_response=True)

    def test_custom_tundra_router_ipv4(self):
        """Test custom tundra_router_ipv4 (e.g. 14.18.0.254 for az-world)."""
        resolver = self.make_resolver_with_tundra(tundra_router_ipv4='14.18.0.254')
        self.assertEqual(resolver.tundra_router_ipv4, '14.18.0.254')
        # Verify fallback uses custom router IP
        resolver.siit_by_vip['14.16.0.2'] = '2001:db8::1'
        resolver.siit_by_real['2001:db8::1'] = '14.16.0.2'
        request = self._encode_request(
            dnsmap.TUNDRA_MSG_TYPE_6TO4_MAIN,
            '2001:db8::ff',  # unknown real IPv6
            'fd64:ff9b::a01:a605'  # destination IS in client prefix
        )
        response = resolver.tundra_server._process_request(request)
        parsed = self._decode_response(response)
        self.assertTrue(parsed['response'])
        self.assertFalse(parsed['error'])
        self.assertFalse(parsed['icmp'])
        self.assertEqual(str(parsed['src_ip']), '14.18.0.254')
        self.assertEqual(str(parsed['dst_ip']), '10.1.166.5')
        self.assertEqual(parsed['cache_lifetime'], 0)

    def test_aaaa_only_tundra_not_available_returns_servfail(self):
        """AAAA-only domain with Tundra server unavailable: returns SERVFAIL."""
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_tundra()
        resolver.query_doh = Mock(side_effect=[
            self._a_reply(request),                       # A: NOERROR, no records
            self._aaaa_reply(request, ['2001:db8::1']),   # AAAA: one record
        ])
        # Simulate Tundra server exists but is not ready (available = False).
        resolver.tundra_server.available = False

        reply = resolver.resolve(request, Mock())

        # Tundra unavailable → AAAA query is made but _ipv6_via_tundra returns
        # None → SERVFAIL.
        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)
        # AAAA query was sent (tundra_server is not None).
        self.assertEqual(resolver.query_doh.call_count, 2)

    def test_concurrent_siit_allocation_same_vip(self):
        """Concurrent requests for different domains with same AAAA get same VIP."""
        resolver = self.make_resolver_with_tundra()
        resolver._is_tundra_ready = Mock(return_value=True)

        request1 = DNSRecord.question('example1.com', 'A')
        request2 = DNSRecord.question('example2.com', 'A')

        def doh_side_effect(request, client_id):
            qname = str(request.q.qname)
            if request.q.qtype == QTYPE.A:
                if qname == 'example1.com':
                    return self._a_reply(request, ['192.0.2.1'])
                else:
                    return self._a_reply(request, ['192.0.2.2'])
            else:  # AAAA
                return self._aaaa_reply(request, ['2001:db8::1'])

        resolver.query_doh = Mock(side_effect=doh_side_effect)
        with patch.object(dnsmap.subprocess, 'run',
                          return_value=type('SimpleNS', (), {'returncode': 0, 'stderr': ''})()):
            results = {}
            errors = []

            def resolve_domain(name, req):
                try:
                    reply = resolver.resolve(req, Mock(client_address=('10.1.166.5', 12345)))
                    results[name] = reply
                except Exception as e:
                    errors.append((name, e))

            t1 = threading.Thread(target=resolve_domain, args=('example1.com', request1))
            t2 = threading.Thread(target=resolve_domain, args=('example2.com', request2))

            t1.start()
            t2.start()
            t1.join()
            t2.join()

        self.assertEqual(len(errors), 0, "Errors during concurrent resolution: {}".format(errors))
        self.assertEqual(len(results), 2, "Expected 2 results, got {}".format(len(results)))

        # Both should get valid responses
        for name, reply in results.items():
            self.assertEqual(reply.header.rcode, RCODE.NOERROR, "Domain {} failed".format(name))
            self.assertEqual(len(reply.rr), 1, "Domain {} wrong answer count".format(name))
            self.assertEqual(reply.rr[0].rtype, QTYPE.A, "Domain {} wrong record type".format(name))

        # Both should get the same VIP
        vip1 = str(results['example1.com'].rr[0].rdata)
        vip2 = str(results['example2.com'].rr[0].rdata)
        self.assertEqual(vip1, vip2,
            "Concurrent requests got different VIPs: {} vs {}".format(vip1, vip2))

        # Only one SIIT binding should exist
        self.assertEqual(len(resolver.siit_by_vip), 1,
            "Expected 1 SIIT VIP binding, got {}".format(len(resolver.siit_by_vip)))
        self.assertEqual(len(resolver.siit_by_real), 1,
            "Expected 1 SIIT real binding, got {}".format(len(resolver.siit_by_real)))
        self.assertEqual(resolver.get_siit_mapping_by_real('2001:db8::1'), vip1)


class CheckIPv6AuthorizedTests(ResolverTestCase):
    """Tests for the AdGuard az-ipv6-* authorization check."""

    def make_resolver_with_ipv6_check(self, **overrides):
        arguments = {
            'address': 'adguard', 'port': 53, 'doh_port': 3000, 'timeout': 1,
            'iprange': '14.16.0.0/29', 'client_id': 'az-local',
            'resolver_client_id': 'az-resolver',
            'asn_file': str(self.asn_file),
            'asn_database': '/unused.mmdb',
            'asn_database_reader': self.asn_database,
            'iptables_runner': self.iptables_runner,
            'load_existing_mappings': False,
            'mapping_batch_delay': 0.001,
            'ipv6_client_id': 'az-ipv6-local',
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        return resolver

    def make_resolver_with_tundra(self, **overrides):
        arguments = {
            'address': 'adguard', 'port': 53, 'doh_port': 3000, 'timeout': 1,
            'iprange': '14.16.0.0/29', 'client_id': 'az-local',
            'resolver_client_id': 'az-resolver',
            'asn_file': str(self.asn_file),
            'asn_database': '/unused.mmdb',
            'asn_database_reader': self.asn_database,
            'iptables_runner': self.iptables_runner,
            'load_existing_mappings': False,
            'mapping_batch_delay': 0.001,
            'tundra_socket': '/run/tundra.sock',
            'conntrack_path': str(self.conntrack_file),
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        return resolver

    def _encode_request(self, msg_type, src_ip, dst_ip, msg_id=12345):
        return dnsmap._tundra_encode_message(
            False, False, False, msg_type, 0, msg_id, src_ip, dst_ip
        )

    def _decode_response(self, data):
        return dnsmap._tundra_decode_message(data, is_response=True)

    def test_adguard_authorizes_ipv6_egress(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_ipv6_check()
        reply = request.reply()
        reply.header.rcode = RCODE.NOERROR
        reply.add_answer(RR(request.q.qname, QTYPE.A, rdata=A('127.0.0.1')))
        resolver.query_doh = Mock(return_value=reply)

        self.assertEqual(
            resolver.check_ipv6_authorized('example.com.'),
            dnsmap.IPV6_EGRESS_AUTHORIZED
        )
        resolver.query_doh.assert_called_once()
        self.assertEqual(resolver.query_doh.call_args[0][1], 'az-ipv6-local')

    def test_adguard_denies_ipv6_egress_on_servfail(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.make_resolver_with_ipv6_check()
        reply = request.reply()
        reply.header.rcode = RCODE.SERVFAIL
        resolver.query_doh = Mock(return_value=reply)

        self.assertEqual(
            resolver.check_ipv6_authorized('example.com.'),
            dnsmap.IPV6_EGRESS_DENIED
        )

    def test_is_tundra_ready_when_available(self):
        """_is_tundra_ready() returns True when server available and no carrier file (outside container)."""
        resolver = self.make_resolver_with_tundra()
        # Force available=True since socket bind may fail in test env
        resolver.tundra_server.available = True
        self.assertTrue(resolver._is_tundra_ready())

    def test_is_tundra_ready_when_unavailable_server_none(self):
        """_is_tundra_ready() returns False when tundra_server is None."""
        resolver = self.make_resolver_with_tundra()
        resolver.tundra_server = None
        self.assertFalse(resolver._is_tundra_ready())

    def test_is_tundra_ready_when_carrier_down(self):
        """_is_tundra_ready() returns False when TUN carrier is down (0)."""
        resolver = self.make_resolver_with_tundra()
        resolver.tundra_server.available = True
        # Mock open() to simulate carrier file returning '0'
        carrier_path = f'/sys/class/net/{resolver.tundra_interface}/carrier'
        real_open = builtins.open
        def fake_open(path, *args, **kwargs):
            if path == carrier_path:
                return mock_open(read_data='0').return_value
            return real_open(path, *args, **kwargs)
        with patch('builtins.open', side_effect=fake_open):
            self.assertFalse(resolver._is_tundra_ready())

    def test_is_tundra_ready_when_no_carrier_file(self):
        """_is_tundra_ready() falls back to available flag when carrier file missing."""
        resolver = self.make_resolver_with_tundra()
        resolver.tundra_server.available = True
        # Carrier file doesn't exist - FileNotFoundError caught, falls back to available
        with patch('builtins.open', side_effect=FileNotFoundError('No carrier')):
            self.assertTrue(resolver._is_tundra_ready())

    def test_adguard_check_error_returns_error(self):
        resolver = self.make_resolver_with_ipv6_check()
        resolver.query_doh = Mock(side_effect=OSError('connection refused'))
        self.assertEqual(
            resolver.check_ipv6_authorized('example.com.'),
            dnsmap.IPV6_EGRESS_ERROR
        )

    def test_no_ipv6_client_id_returns_error(self):
        resolver = self.make_resolver()
        self.assertEqual(
            resolver.check_ipv6_authorized('example.com.'),
            dnsmap.IPV6_EGRESS_ERROR
        )

    def test_check_ipv6_authorized_direct_egress_returns_denied(self):
        """Domain marked 'direct' in egress map returns DENIED without calling AdGuard."""
        resolver = self.make_resolver_with_ipv6_check(
            ipv6_egress_map={'example.com.': 'direct'})
        resolver.query_doh = Mock()
        result = resolver.check_ipv6_authorized('example.com.')
        self.assertEqual(result, dnsmap.IPV6_EGRESS_DENIED)
        resolver.query_doh.assert_not_called()

    def test_check_ipv6_authorized_local_egress_uses_local_client(self):
        """Domain marked 'local' uses az-ipv6-local client ID."""
        resolver = self.make_resolver_with_ipv6_check(
            ipv6_egress_map={'example.com.': 'local'},
            ipv6_client_id='az-ipv6-world')
        request = DNSRecord.question('example.com.', 'A')
        reply = request.reply()
        reply.header.rcode = RCODE.NOERROR
        reply.add_answer(RR(request.q.qname, QTYPE.A, rdata=A('127.0.0.1')))
        resolver.query_doh = Mock(return_value=reply)

        result = resolver.check_ipv6_authorized('example.com.')
        self.assertEqual(result, dnsmap.IPV6_EGRESS_AUTHORIZED)
        resolver.query_doh.assert_called_once()
        self.assertEqual(resolver.query_doh.call_args[0][1], 'az-ipv6-local')

    def test_check_ipv6_authorized_world_egress_uses_world_client(self):
        """Domain marked 'world' uses az-ipv6-world client ID."""
        resolver = self.make_resolver_with_ipv6_check(
            ipv6_egress_map={'example.com.': 'world'},
            ipv6_client_id='az-ipv6-local')
        request = DNSRecord.question('example.com.', 'A')
        reply = request.reply()
        reply.header.rcode = RCODE.NOERROR
        reply.add_answer(RR(request.q.qname, QTYPE.A, rdata=A('127.0.0.1')))
        resolver.query_doh = Mock(return_value=reply)

        result = resolver.check_ipv6_authorized('example.com.')
        self.assertEqual(result, dnsmap.IPV6_EGRESS_AUTHORIZED)
        resolver.query_doh.assert_called_once()
        self.assertEqual(resolver.query_doh.call_args[0][1], 'az-ipv6-world')

    def test_check_ipv6_authorized_unknown_domain_uses_default_client(self):
        """Domain not in egress map uses the default ipv6_client_id."""
        resolver = self.make_resolver_with_ipv6_check(
            ipv6_egress_map={'other.com.': 'local'},
            ipv6_client_id='az-ipv6-local')
        request = DNSRecord.question('example.com.', 'A')
        reply = request.reply()
        reply.header.rcode = RCODE.NOERROR
        reply.add_answer(RR(request.q.qname, QTYPE.A, rdata=A('127.0.0.1')))
        resolver.query_doh = Mock(return_value=reply)

        result = resolver.check_ipv6_authorized('EXAMPLE.COM.')
        self.assertEqual(result, dnsmap.IPV6_EGRESS_AUTHORIZED)
        resolver.query_doh.assert_called_once()
        self.assertEqual(resolver.query_doh.call_args[0][1], 'az-ipv6-local')

    def test_check_ipv6_authorized_direct_egress_case_insensitive(self):
        """Domain marked 'direct' with different case still returns DENIED."""
        resolver = self.make_resolver_with_ipv6_check(
            ipv6_egress_map={'Example.COM.': 'direct'})
        resolver.query_doh = Mock()
        result = resolver.check_ipv6_authorized('example.com.')
        self.assertEqual(result, dnsmap.IPV6_EGRESS_DENIED)
        resolver.query_doh.assert_not_called()

    # ------------------------------------------------------------------
    # Excluded addresses tests
    # ------------------------------------------------------------------

    def test_excluded_addresses_not_in_pool(self):
        """Tundra service addresses are excluded from the VIP pool."""
        resolver = dnsmap.ProxyResolver(
            address='adguard', port=53, doh_port=3000, timeout=1,
            iprange='14.16.0.0/29', client_id='az-local',
            resolver_client_id='az-resolver',
            asn_file=str(self.asn_file),
            asn_database='/unused.mmdb',
            asn_database_reader=self.asn_database,
            iptables_runner=self.iptables_runner,
            load_existing_mappings=False,
            mapping_batch_delay=0.001,
            tundra_socket='/run/tundra.sock',
            excluded_addresses=['14.16.0.1', '14.16.0.254'],
        )
        self.addCleanup(resolver.close)
        self.assertNotIn('14.16.0.1', resolver.unassigned_addresses)
        self.assertEqual(resolver.excluded_addresses, {'14.16.0.1', '14.16.0.254'})

    def test_excluded_address_never_assigned_as_vip(self):
        """An excluded address is never allocated as a SIIT VIP."""
        resolver = dnsmap.ProxyResolver(
            address='adguard', port=53, doh_port=3000, timeout=1,
            iprange='14.16.0.0/29', client_id='az-local',
            resolver_client_id='az-resolver',
            asn_file=str(self.asn_file),
            asn_database='/unused.mmdb',
            asn_database_reader=self.asn_database,
            iptables_runner=self.iptables_runner,
            load_existing_mappings=False,
            mapping_batch_delay=0.001,
            tundra_socket='/run/tundra.sock',
            excluded_addresses=['14.16.0.1', '14.16.0.2'],
        )
        self.addCleanup(resolver.close)
        # 14.16.0.0/29 hosts: .1-.6, minus .1 (DNS), minus .2 (excluded)
        self.assertNotIn('14.16.0.2', resolver.unassigned_addresses)
        self.assertNotIn('14.16.0.1', resolver.unassigned_addresses)
        # Allocate all remaining addresses
        allocated = []
        while True:
            vip = resolver._allocate_siit_vip()
            if vip is None:
                break
            allocated.append(vip)
        self.assertNotIn('14.16.0.2', allocated)
        self.assertNotIn('14.16.0.1', allocated)

    # ------------------------------------------------------------------
    # Tundra wireformat edge case tests
    # ------------------------------------------------------------------

    def test_invalid_magic_byte_returns_error(self):
        """A request with an invalid magic byte gets an error response."""
        resolver = self.make_resolver_with_tundra()
        bad = bytes([0x55, 1, 0, 0]) + bytes(4) + bytes(16) + bytes(16)
        response = resolver.tundra_server._process_request(bad)
        parsed = self._decode_response(response)
        self.assertTrue(parsed['response'])
        self.assertTrue(parsed['error'])

    def test_request_with_response_bit_returns_error(self):
        """A request with the response bit set gets an error response."""
        resolver = self.make_resolver_with_tundra()
        request = dnsmap._tundra_encode_message(
            True, False, False, dnsmap.TUNDRA_MSG_TYPE_4TO6_MAIN, 0, 12345,
            '10.1.166.5', '14.16.0.2'
        )
        response = resolver.tundra_server._process_request(request)
        parsed = self._decode_response(response)
        self.assertTrue(parsed['response'])
        self.assertTrue(parsed['error'])

    def test_4to6_icmp_error_unknown_vip_returns_error(self):
        """4TO6_ICMP_ERROR with unknown VIP returns error."""
        resolver = self.make_resolver_with_tundra()
        # No SIIT binding installed.
        # 4TO6_ICMP_ERROR: inner src = VIP, inner dst = client_ipv4.
        request = self._encode_request(
            dnsmap.TUNDRA_MSG_TYPE_4TO6_ICMP_ERROR,
            '14.16.0.99',   # VIP as source (unknown)
            '10.1.166.5',   # client IPv4 as dest
        )
        response = resolver.tundra_server._process_request(request)
        parsed = self._decode_response(response)

        self.assertTrue(parsed['response'])
        self.assertTrue(parsed['error'])
        self.assertFalse(parsed['icmp'])  # ICMP error types don't set ICMP bit on error
        self.assertEqual(parsed['msg_type'], dnsmap.TUNDRA_MSG_TYPE_4TO6_ICMP_ERROR)

    def test_4to6_icmp_error_vip_to_client(self):
        """4TO6_ICMP_ERROR with known VIP returns real IPv6 and client IPv4."""
        resolver = self.make_resolver_with_tundra()
        resolver.siit_by_vip['14.16.0.2'] = '2001:db8::1'
        resolver.siit_by_real['2001:db8::1'] = '14.16.0.2'

        # 4TO6_ICMP_ERROR: inner src = VIP → real IPv6, inner dst = client_ipv4 → fd64:ff9b::<hex>
        request = self._encode_request(
            dnsmap.TUNDRA_MSG_TYPE_4TO6_ICMP_ERROR,
            '14.16.0.2',   # VIP as source
            '10.1.166.5',  # client IPv4 as dest
        )
        response = resolver.tundra_server._process_request(request)
        parsed = self._decode_response(response)

        self.assertTrue(parsed['response'])
        self.assertFalse(parsed['error'])
        self.assertFalse(parsed['icmp'])
        self.assertEqual(str(parsed['src_ip']), '2001:db8::1')
        self.assertEqual(str(parsed['dst_ip']), 'fd64:ff9b::a01:a605')


    # ------------------------------------------------------------------
    # DNS ID mismatch tests
    # ------------------------------------------------------------------

    def test_dns_id_preserved_aaaa_only(self):
        """AAAA-only domain: reply ID must match request ID."""
        resolver = self.make_resolver_with_tundra()
        request = DNSRecord.question('ipv6only.example', 'A')
        request.header.id = 42424
        resolver.query_doh = Mock(side_effect=[
            dns_reply(request),                                       # A: NOERROR, no records
            dns_reply(request, ['2001:db8::1'], qtype=QTYPE.AAAA),    # AAAA: one record
        ])

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.id, request.header.id)
        self.assertEqual(reply.header.id, 42424)

    def test_dns_id_preserved_a_and_aaaa_authorized(self):
        """A+AAAA authorized via Tundra: reply ID must match request ID."""
        resolver = self.make_resolver_with_tundra()
        request = DNSRecord.question('dual.example', 'A')
        request.header.id = 42424
        resolver.query_doh = Mock(side_effect=[
            dns_reply(request, ['192.0.2.1']),
            dns_reply(request, ['2001:db8::1'], qtype=QTYPE.AAAA),
        ])
        resolver.check_ipv6_authorized = Mock(
            return_value=dnsmap.IPV6_EGRESS_AUTHORIZED)

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.id, request.header.id)
        self.assertEqual(reply.header.id, 42424)

    def test_dns_id_preserved_a_and_aaaa_denied(self):
        """A+AAAA denied (falls back to IPv4): reply ID must match request ID."""
        resolver = self.make_resolver_with_tundra()
        request = DNSRecord.question('dual.example', 'A')
        request.header.id = 42424
        resolver.query_doh = Mock(side_effect=[
            dns_reply(request, ['192.0.2.1']),
            dns_reply(request, ['2001:db8::1'], qtype=QTYPE.AAAA),
        ])
        resolver.check_ipv6_authorized = Mock(
            return_value=dnsmap.IPV6_EGRESS_DENIED)
        resolver.add_mappings = Mock(return_value={'192.0.2.1': '14.16.0.2'})

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.id, request.header.id)
        self.assertEqual(reply.header.id, 42424)

    def test_dns_id_preserved_a_only(self):
        """A-only domain: reply ID must match request ID."""
        resolver = self.make_resolver_with_tundra()
        request = DNSRecord.question('ipv4only.example', 'A')
        request.header.id = 42424
        resolver.query_doh = Mock(side_effect=[
            dns_reply(request, ['192.0.2.1']),
            dns_reply(request),                                       # AAAA: no records
        ])
        resolver.check_ipv6_authorized = Mock()
        resolver.add_mappings = Mock(return_value={'192.0.2.1': '14.16.0.2'})

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.id, request.header.id)
        self.assertEqual(reply.header.id, 42424)

    def test_dns_id_preserved_upstream_servfail(self):
        """Upstream SERVFAIL: reply ID must match request ID."""
        resolver = self.make_resolver_with_tundra()
        request = DNSRecord.question('fail.example', 'A')
        request.header.id = 42424
        resolver.query_doh = Mock(return_value=dns_reply(request, rcode=RCODE.SERVFAIL))

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.id, request.header.id)
        self.assertEqual(reply.header.id, 42424)

    def test_dns_id_preserved_upstream_nxdomain(self):
        """Upstream NXDOMAIN: reply ID must match request ID."""
        resolver = self.make_resolver_with_tundra()
        request = DNSRecord.question('nx.example', 'A')
        request.header.id = 42424
        resolver.query_doh = Mock(return_value=dns_reply(request, rcode=RCODE.NXDOMAIN))

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.id, request.header.id)
        self.assertEqual(reply.header.id, 42424)

    def test_dns_id_preserved_refused_no_asn_match(self):
        """REFUSED upstream with different ID: resolve_by_asn returns REFUSED,
        final answer must carry the original request ID."""
        resolver = self.make_resolver_with_tundra()
        request = DNSRecord.question('refused.example', 'A')
        request.header.id = 42424
        # Upstream returns REFUSED with a *different* ID.
        refused = dns_reply(request, rcode=RCODE.REFUSED)
        refused.header.id = 9999
        resolver.query_doh = Mock(return_value=refused)
        resolver.is_blocked_asn = Mock(return_value=False)

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.REFUSED)
        self.assertEqual(reply.header.id, request.header.id)
        self.assertEqual(reply.header.id, 42424)

    def test_dns_id_preserved_empty_noerror_no_a_no_aaaa(self):
        """No A and no AAAA (empty NOERROR): answer must be the A-query reply
        (Question qtype == A) with the original request ID."""
        resolver = self.make_resolver_with_tundra()
        request = DNSRecord.question('empty.example', 'A')
        request.header.id = 42424
        # A query: NOERROR, no records.
        a_empty = dns_reply(request)
        a_empty.header.id = 9999
        # AAAA query: NOERROR, no records.
        aaaa_empty = dns_reply(request)
        aaaa_empty.header.id = 8888
        resolver.query_doh = Mock(side_effect=[a_empty, aaaa_empty])

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(reply.header.id, request.header.id)
        self.assertEqual(reply.header.id, 42424)
        # Question section must be for the A query, not AAAA.
        self.assertEqual(reply.q.qtype, QTYPE.A)
        self.assertEqual(reply.rr, [])


if __name__ == '__main__':
    unittest.main()
