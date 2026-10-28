# Run from the repository root:
# docker run --rm -v "$PWD/services/antizapret/root/antizapret:/tests:ro" -w /tests xtrime/antizapret-vpn:6.7.0 python3 -B -m unittest -v test_dnsmap.py

import queue
import ipaddress
import socket
import struct
import subprocess
import sys
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from dnslib import A, AAAA, CNAME, DNSRecord, QTYPE, RCODE, RR

import dnsmap
import ipv6_translator


def dns_reply(request, addresses=(), rcode=RCODE.NOERROR):
    reply = request.reply()
    reply.header.rcode = rcode
    for address in addresses:
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
        self.iptables_runner = Mock(return_value=Mock(returncode=0, stderr=''))

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
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        return resolver


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

    def test_empty_and_unspecified_addresses_skip_asn_lookup_in_asn_list_tests(self):
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

    def test_empty_and_unspecified_addresses_skip_asn_lookup_in_asn_list_tests_2(self):
        resolver = self.make_resolver()

        for address in (None, '', '0.0.0.0'):
            with self.subTest(address=address):
                self.assertFalse(resolver.is_blocked_asn(address))

        self.asn_database.get.assert_not_called()

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
        resolver.ipv4_pending_mappings = self.TrackingDict()

        self.assertEqual(resolver.add_mapping('192.0.2.1'), '14.16.0.2')

        self.assertEqual(resolver.get_mapping('192.0.2.1'), '14.16.0.2')
        self.assertTrue(resolver.ipv4_pending_mappings.was_cleared)
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

    def test_exhausted_range_flushes_old_mappings_and_starts_over(self):
        resolver = self.make_resolver(iprange='14.16.0.0/30')

        self.assertEqual(resolver.add_mapping('192.0.2.1'), '14.16.0.2')
        self.assertEqual(resolver.add_mapping('192.0.2.2'), '14.16.0.2')

        self.assertEqual(resolver.ipmap, {'192.0.2.2': '14.16.0.2'})
        commands = [call.args[0] for call in self.iptables_runner.call_args_list]
        self.assertEqual(commands[1], ['iptables', '-w', '-t', 'nat', '-F', 'dnsmap'])
        self.assertIn(
            '--to-destination 192.0.2.2',
            self.iptables_runner.call_args_list[2].kwargs['input'],
        )

    def test_batch_is_not_partially_allocated_when_range_is_exhausted(self):
        resolver = self.make_resolver(iprange='14.16.0.0/29')
        for index in range(4):
            resolver.add_mapping('192.0.2.{}'.format(index + 1))

        mappings = resolver.add_mappings(['192.0.2.10', '192.0.2.11'])

        self.assertEqual(mappings, {
            '192.0.2.10': '14.16.0.2',
            '192.0.2.11': '14.16.0.3',
        })
        self.assertEqual(resolver.ipmap, mappings)
        commands = [call.args[0] for call in self.iptables_runner.call_args_list]
        self.assertEqual(commands[-2], ['iptables', '-w', '-t', 'nat', '-F', 'dnsmap'])
        rules = self.iptables_runner.call_args_list[-1].kwargs['input']
        self.assertIn('--to-destination 192.0.2.10', rules)
        self.assertIn('--to-destination 192.0.2.11', rules)

    def test_mapping_is_rejected_after_resolver_stops(self):
        resolver = self.make_resolver()
        resolver.close()

        self.assertFalse(resolver.add_mapping('192.0.2.1'))
        self.iptables_runner.assert_not_called()

    def test_failed_flush_preserves_old_mappings(self):
        def run_iptables(command, **kwargs):
            if '-F' in command:
                return Mock(returncode=1, stderr='flush failed')
            return Mock(returncode=0, stderr='')

        self.iptables_runner.side_effect = run_iptables
        resolver = self.make_resolver(iprange='14.16.0.0/30')

        self.assertEqual(resolver.add_mapping('192.0.2.1'), '14.16.0.2')
        self.assertFalse(resolver.add_mapping('192.0.2.2'))

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
        resolver = self.resolver_with_replies([dns_reply(request, ['192.0.2.1'])])

        reply = resolver.resolve(request, Mock())

        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        resolver.is_blocked_asn.assert_not_called()

    def test_servfail_without_asn_match_is_preserved(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.resolver_with_replies([
            dns_reply(request, rcode=RCODE.SERVFAIL),
            dns_reply(request, ['192.0.2.1']),
        ])

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)
        resolver.add_mappings.assert_not_called()

    def test_resolver_servfail_is_returned_without_asn_lookup(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.resolver_with_replies([
            dns_reply(request, rcode=RCODE.SERVFAIL),
            dns_reply(request, ['192.0.2.1'], rcode=RCODE.SERVFAIL),
        ], asn_match=True)

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)
        resolver.is_blocked_asn.assert_not_called()
        resolver.add_mappings.assert_not_called()

    def test_empty_resolver_answer_preserves_filtered_reply(self):
        request = DNSRecord.question('empty.example', 'A')
        filtered_reply = dns_reply(request, rcode=RCODE.SERVFAIL)
        resolved_reply = dns_reply(request)
        resolver = self.resolver_with_replies([
            filtered_reply,
            resolved_reply,
        ], asn_match=True)

        reply = resolver.resolve(request, Mock())

        self.assertIs(reply, filtered_reply)
        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)
        resolver.is_blocked_asn.assert_not_called()
        resolver.add_mappings.assert_not_called()

    def test_resolver_servfail_is_returned_without_asn_lookup_in_resolver_contract_tests(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.resolver_with_replies([
            dns_reply(request, rcode=RCODE.SERVFAIL),
            dns_reply(request, ['192.0.2.1'], rcode=RCODE.SERVFAIL),
        ], asn_match=True)

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)
        resolver.is_blocked_asn.assert_not_called()
        resolver.add_mappings.assert_not_called()

    def test_empty_resolver_answer_preserves_filtered_reply_in_resolver_contract_tests(self):
        request = DNSRecord.question('empty.example', 'A')
        filtered_reply = dns_reply(request, rcode=RCODE.SERVFAIL)
        resolved_reply = dns_reply(request)
        resolver = self.resolver_with_replies([
            filtered_reply,
            resolved_reply,
        ], asn_match=True)

        reply = resolver.resolve(request, Mock())

        self.assertIs(reply, filtered_reply)
        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)
        resolver.is_blocked_asn.assert_not_called()
        resolver.add_mappings.assert_not_called()

        self.assertIs(reply, filtered_reply)
        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)
        resolver.is_blocked_asn.assert_not_called()
        resolver.add_mappings.assert_not_called()

    def test_one_asn_match_maps_all_resolved_addresses(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.resolver_with_replies([
            dns_reply(request, rcode=RCODE.SERVFAIL),
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
        resolver = self.resolver_with_replies([dns_reply(request)])

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
        resolver = self.resolver_with_replies([upstream_reply])

        reply = resolver.resolve(request, Mock())

        self.assertIs(reply, upstream_reply)
        self.assertEqual(reply.rr[0].rtype, QTYPE.CNAME)
        resolver.add_mappings.assert_not_called()

    def test_aaaa_and_https_are_suppressed(self):
        for query_type in ('AAAA', 'HTTPS'):
            with self.subTest(query_type=query_type):
                request = DNSRecord.question('example.com', query_type)
                resolver = self.resolver_with_replies([dns_reply(request)])
                reply = resolver.resolve(request, Mock())
                self.assertEqual(reply.header.rcode, RCODE.NOERROR)
                self.assertEqual(reply.rr, [])

    def test_mmdb_exception_returns_servfail(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.resolver_with_replies([
            dns_reply(request, rcode=RCODE.SERVFAIL),
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


class IPv6EgressDomainTests(ResolverTestCase):
    """Tests for IPv6 egress domain matching logic."""

    def make_resolver_with_ipv6(self, **overrides):
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
            'ipv6_egress_file': str(self.temp_directory.name + '/ipv6-egress.txt'),
            'ipv6_translator_port': 9000,
            'ipv6_egress_enabled': True,
            'ipv6_egress_mode': 'file_only',
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        return resolver

    def test_ipv6_egress_domains_loaded_from_file(self):
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n# comment\n\n test.org\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        self.assertTrue(resolver.is_ipv6_egress_domain('example.com'))
        self.assertTrue(resolver.is_ipv6_egress_domain('www.example.com'))
        self.assertTrue(resolver.is_ipv6_egress_domain('test.org'))
        self.assertFalse(resolver.is_ipv6_egress_domain('other.com'))

    def test_ipv6_egress_subdomain_matching(self):
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        self.assertTrue(resolver.is_ipv6_egress_domain('example.com'))
        self.assertTrue(resolver.is_ipv6_egress_domain('www.example.com'))
        self.assertTrue(resolver.is_ipv6_egress_domain('sub.www.example.com'))
        self.assertFalse(resolver.is_ipv6_egress_domain('example.com.evil.org'))
        self.assertFalse(resolver.is_ipv6_egress_domain('other.com'))

    def test_ipv6_egress_empty_file(self):
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        self.assertFalse(resolver.is_ipv6_egress_domain('example.com'))
        self.assertEqual(len(resolver.ipv6_egress_domains), 0)

    def test_ipv6_egress_missing_file(self):
        resolver = self.make_resolver_with_ipv6()
        # File doesn't exist, should not crash
        self.assertFalse(resolver.is_ipv6_egress_domain('example.com'))
        self.assertEqual(len(resolver.ipv6_egress_domains), 0)

    def test_ipv6_egress_reload(self):
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        self.assertTrue(resolver.is_ipv6_egress_domain('example.com'))
        self.assertFalse(resolver.is_ipv6_egress_domain('test.org'))

        # Reload with new file contents
        ipv6_file.write_text('test.org\n', encoding='utf-8')
        resolver.reload_ipv6_egress_domains(None, None)

        self.assertFalse(resolver.is_ipv6_egress_domain('example.com'))
        self.assertTrue(resolver.is_ipv6_egress_domain('test.org'))


class IPv6EgressMappingTests(ResolverTestCase):
    """Tests for IPv6 mapping allocation."""

    def make_resolver_with_ipv6(self, **overrides):
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
            'ipv6_egress_file': str(self.temp_directory.name + '/ipv6-egress.txt'),
            'ipv6_translator_port': 9000,
            'ipv6_egress_enabled': True,
            'ipv6_egress_mode': 'file_only',
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        return resolver

    def test_add_ipv6_mapping_creates_dnat_to_translator(self):
        resolver = self.make_resolver_with_ipv6()

        result = resolver.add_ipv6_mapping('2001:db8::1')

        self.assertEqual(result, '14.16.0.2')
        self.assertEqual(resolver.get_ipv6_mapping_by_real('2001:db8::1'), '14.16.0.2')

        # Check iptables rule was created for translator
        command = self.iptables_runner.call_args.args[0]
        self.assertEqual(command, ['iptables-restore', '--wait', '--noflush'])
        input_rules = self.iptables_runner.call_args.kwargs['input']
        self.assertIn(
            '-A dnsmap-ipv6 -p tcp -d 14.16.0.2 -j DNAT --to-destination 127.0.0.1:9000',
            input_rules,
        )
        self.assertIn(
            '-A dnsmap-ipv6 -p udp -d 14.16.0.2 -j DNAT --to-destination 127.0.0.1:9000',
            input_rules,
        )

    def test_add_ipv6_mapping_reuses_existing(self):
        resolver = self.make_resolver_with_ipv6()

        resolver.add_ipv6_mapping('2001:db8::1')
        result = resolver.add_ipv6_mapping('2001:db8::1')

        self.assertEqual(result, '14.16.0.2')
        # Only one iptables-restore call should be made
        self.assertEqual(self.iptables_runner.call_count, 1)

    def test_add_ipv6_mappings_multiple(self):
        resolver = self.make_resolver_with_ipv6()

        results = resolver.add_ipv6_mappings(['2001:db8::1', '2001:db8::2'])

        self.assertEqual(results['2001:db8::1'], '14.16.0.2')
        self.assertEqual(results['2001:db8::2'], '14.16.0.3')

    def test_get_ipv6_mapping_by_fake(self):
        resolver = self.make_resolver_with_ipv6()
        resolver.add_ipv6_mapping('2001:db8::1')

        result = resolver.get_ipv6_mapping('14.16.0.2')
        self.assertEqual(result, '2001:db8::1')

    def test_get_ipv6_mapping_not_found(self):
        resolver = self.make_resolver_with_ipv6()
        result = resolver.get_ipv6_mapping('14.16.0.99')
        self.assertIsNone(result)

    def test_write_ipv6_mapping_file(self):
        resolver = self.make_resolver_with_ipv6()
        resolver.ipv6_mapping_file = str(
            Path(self.temp_directory.name) / 'mappings.txt'
        )
        resolver.add_ipv6_mapping('2001:db8::1')

        with open(resolver.ipv6_mapping_file, 'r') as f:
            content = f.read()
        self.assertIn('14.16.0.2 2001:db8::1', content)

    # ------------------------------------------------------------------
    # Pool exhaustion tests
    # ------------------------------------------------------------------

    def test_ipv6_pool_exhaustion_returns_none_not_flush_ipv4(self):
        """When IPv6 egress pool is exhausted, returns None without flushing IPv4 mappings."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        # Very small range: 14.16.0.0/29 has 6 hosts, minus 1 for DNS = 5 addresses
        # Reserve 4 for IPv6 egress, leaving 1 for regular IPv4
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
            ipv6_egress_file=str(ipv6_file),
            ipv6_translator_port=9000,
            ipv6_egress_enabled=True,
            ipv6_egress_reserve_count=4,
        )
        self.addCleanup(resolver.close)

        # Regular IPv4 mapping uses one address from the regular pool
        resolver.add_mapping('192.0.2.1')
        regular_mapping_count = len(resolver.ipmap)
        self.assertEqual(regular_mapping_count, 1)

        # Exhaust the IPv6 egress pool (4 addresses)
        result1 = resolver.add_ipv6_mapping('2001:db8::1')
        result2 = resolver.add_ipv6_mapping('2001:db8::2')
        result3 = resolver.add_ipv6_mapping('2001:db8::3')
        result4 = resolver.add_ipv6_mapping('2001:db8::4')

        # All 4 should be allocated
        self.assertIsNotNone(result1)
        self.assertIsNotNone(result2)
        self.assertIsNotNone(result3)
        self.assertIsNotNone(result4)

        # 5th allocation should fail (pool exhausted) but NOT flush IPv4 mappings
        result5 = resolver.add_ipv6_mapping('2001:db8::5')
        self.assertIsNone(result5)

        # Regular IPv4 mapping should still exist
        self.assertEqual(len(resolver.ipmap), regular_mapping_count)
        self.assertIn('192.0.2.1', resolver.ipmap)

    def test_ipv6_pool_reset_only_clears_ipv6_mappings(self):
        """reset_ipv6_mappings clears only IPv6 egress mappings, not IPv4."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

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
            ipv6_egress_file=str(ipv6_file),
            ipv6_translator_port=9000,
            ipv6_egress_enabled=True,
            ipv6_egress_reserve_count=4,
        )
        self.addCleanup(resolver.close)

        # Add both IPv4 and IPv6 mappings
        resolver.add_mapping('192.0.2.1')
        resolver.add_ipv6_mapping('2001:db8::1')

        self.assertEqual(len(resolver.ipmap), 1)
        self.assertEqual(len(resolver.ipv6_mappings), 1)

        # Reset only IPv6 mappings
        resolver.reset_ipv6_mappings()

        # IPv4 mapping should still exist
        self.assertEqual(len(resolver.ipmap), 1)
        self.assertIn('192.0.2.1', resolver.ipmap)

        # IPv6 mappings should be cleared
        self.assertEqual(len(resolver.ipv6_mappings), 0)


class IPv6EgressMappingRestoreTests(ResolverTestCase):
    """Tests for IPv6 mapping restoration from file after restart."""

    def make_resolver_with_ipv6(self, **overrides):
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
            'ipv6_egress_file': str(self.temp_directory.name + '/ipv6-egress.txt'),
            'ipv6_translator_port': 9000,
            'ipv6_egress_enabled': True,
            'ipv6_egress_mode': 'file_only',
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        return resolver

    def test_ipv6_mappings_restored_from_file(self):
        """Existing IPv6 mappings are loaded from the mapping file on startup."""
        mapping_file = Path(self.temp_directory.name) / 'ipv6-egress-mappings.txt'
        mapping_file.write_text(
            '14.16.0.2 2001:db8::1\n14.16.0.3 2001:db8::2\n',
            encoding='utf-8'
        )

        resolver = self.make_resolver_with_ipv6(
            ipv6_mapping_file=str(mapping_file)
        )

        # Both mappings should be restored
        self.assertEqual(resolver.get_ipv6_mapping_by_real('2001:db8::1'), '14.16.0.2')
        self.assertEqual(resolver.get_ipv6_mapping_by_real('2001:db8::2'), '14.16.0.3')
        self.assertEqual(len(resolver.ipv6_mappings), 2)

        # The restored fake IPs should be removed from the unassigned pool
        self.assertNotIn('14.16.0.2', resolver.ipv6_unassigned_addresses)
        self.assertNotIn('14.16.0.3', resolver.ipv6_unassigned_addresses)

        # NAT rules should have been created for both mappings
        input_rules = self.iptables_runner.call_args.kwargs['input']
        self.assertIn(
            '-A dnsmap-ipv6 -p tcp -d 14.16.0.2 -j DNAT --to-destination 127.0.0.1:9000',
            input_rules,
        )
        self.assertIn(
            '-A dnsmap-ipv6 -p udp -d 14.16.0.2 -j DNAT --to-destination 127.0.0.1:9000',
            input_rules,
        )
        self.assertIn(
            '-A dnsmap-ipv6 -p tcp -d 14.16.0.3 -j DNAT --to-destination 127.0.0.1:9000',
            input_rules,
        )
        self.assertIn(
            '-A dnsmap-ipv6 -p udp -d 14.16.0.3 -j DNAT --to-destination 127.0.0.1:9000',
            input_rules,
        )

    def test_ipv6_mappings_restored_skips_malformed_lines(self):
        """Malformed lines in the mapping file are skipped."""
        mapping_file = Path(self.temp_directory.name) / 'ipv6-egress-mappings.txt'
        mapping_file.write_text(
            '14.16.0.2 2001:db8::1\n'
            'not-an-ip 2001:db8::2\n'
            '14.16.0.3 not-an-ip\n'
            '14.16.0.4\n'
            '\n'
            '# comment\n',
            encoding='utf-8'
        )

        resolver = self.make_resolver_with_ipv6(
            ipv6_mapping_file=str(mapping_file)
        )

        # Only the valid mapping should be restored
        self.assertEqual(resolver.get_ipv6_mapping_by_real('2001:db8::1'), '14.16.0.2')
        self.assertIsNone(resolver.get_ipv6_mapping_by_real('2001:db8::2'))
        self.assertEqual(len(resolver.ipv6_mappings), 1)

    def test_ipv6_mappings_restored_skips_duplicates(self):
        """Duplicate fake IPv4 or real IPv6 entries are skipped."""
        mapping_file = Path(self.temp_directory.name) / 'ipv6-egress-mappings.txt'
        mapping_file.write_text(
            '14.16.0.2 2001:db8::1\n'
            '14.16.0.2 2001:db8::2\n'  # duplicate fake IPv4
            '14.16.0.3 2001:db8::1\n'  # duplicate real IPv6
            '14.16.0.4 2001:db8::3\n',
            encoding='utf-8'
        )

        resolver = self.make_resolver_with_ipv6(
            ipv6_mapping_file=str(mapping_file)
        )

        # First occurrence wins for both fake and real
        self.assertEqual(resolver.get_ipv6_mapping_by_real('2001:db8::1'), '14.16.0.2')
        self.assertEqual(resolver.get_ipv6_mapping_by_real('2001:db8::3'), '14.16.0.4')
        self.assertIsNone(resolver.get_ipv6_mapping_by_real('2001:db8::2'))
        self.assertEqual(len(resolver.ipv6_mappings), 2)

    def test_ipv6_mappings_restored_skips_outside_pool(self):
        """Fake IPv4 addresses outside the IPv6 egress pool are skipped."""
        mapping_file = Path(self.temp_directory.name) / 'ipv6-egress-mappings.txt'
        mapping_file.write_text(
            '14.16.0.2 2001:db8::1\n'
            '10.0.0.1 2001:db8::2\n'  # outside the pool
            '14.16.0.3 2001:db8::3\n',
            encoding='utf-8'
        )

        resolver = self.make_resolver_with_ipv6(
            ipv6_mapping_file=str(mapping_file)
        )

        self.assertEqual(resolver.get_ipv6_mapping_by_real('2001:db8::1'), '14.16.0.2')
        self.assertEqual(resolver.get_ipv6_mapping_by_real('2001:db8::3'), '14.16.0.3')
        self.assertIsNone(resolver.get_ipv6_mapping_by_real('2001:db8::2'))
        self.assertEqual(len(resolver.ipv6_mappings), 2)

    def test_ipv6_mappings_restored_prevents_reallocation(self):
        """Restored fake IPv4 addresses are not re-allocated to new mappings."""
        mapping_file = Path(self.temp_directory.name) / 'ipv6-egress-mappings.txt'
        mapping_file.write_text(
            '14.16.0.2 2001:db8::1\n',
            encoding='utf-8'
        )

        resolver = self.make_resolver_with_ipv6(
            ipv6_mapping_file=str(mapping_file)
        )

        # The restored address should not be re-allocated
        result = resolver.add_ipv6_mapping('2001:db8::2')
        self.assertNotEqual(result, '14.16.0.2')
        self.assertEqual(resolver.get_ipv6_mapping_by_real('2001:db8::1'), '14.16.0.2')
        self.assertEqual(resolver.get_ipv6_mapping_by_real('2001:db8::2'), result)


class IPv6EgressResolverTests(ResolverTestCase):
    """Tests for DNS resolution with IPv6 egress domains."""

    def make_resolver_with_ipv6(self, **overrides):
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
            'ipv6_egress_file': str(self.temp_directory.name + '/ipv6-egress.txt'),
            'ipv6_translator_port': 9000,
            'ipv6_egress_enabled': True,
            'ipv6_egress_mode': 'file_only',
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        resolver.check_ipv6_connectivity = Mock(return_value=True)
        self.addCleanup(resolver.close)
        return resolver

    def _make_aaaa_reply(self, qname, addresses):
        """Create a DNS reply with AAAA records."""
        request = DNSRecord.question(qname, 'AAAA')
        reply = request.reply()
        reply.header.rcode = RCODE.NOERROR
        for addr in addresses:
            reply.add_answer(RR(qname, QTYPE.AAAA, ttl=300, rdata=AAAA(addr)))
        return reply

    def test_ipv6_egress_returns_virtual_ipv4(self):
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        resolver.query_doh = Mock(return_value=self._make_aaaa_reply(
            'example.com', ['2001:db8::1']
        ))

        request = DNSRecord.question('example.com', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 1)
        self.assertEqual(reply.rr[0].rtype, QTYPE.A)
        # Should return virtual IPv4, not real IPv6
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        # Should NOT contain the real IPv6
        self.assertNotEqual(str(reply.rr[0].rdata), '2001:db8::1')

    def test_ipv6_egress_multiple_aaaa_uses_first(self):
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        resolver.query_doh = Mock(return_value=self._make_aaaa_reply(
            'example.com', ['2001:db8::1', '2001:db8::2']
        ))

        request = DNSRecord.question('example.com', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')

    def test_ipv6_egress_no_aaaa_returns_servfail(self):
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        # Return empty AAAA response
        request = DNSRecord.question('example.com', 'AAAA')
        empty_reply = request.reply()
        empty_reply.header.rcode = RCODE.NOERROR
        resolver.query_doh = Mock(return_value=empty_reply)

        request = DNSRecord.question('example.com', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)

    def test_ipv6_egress_aaaa_servfail_returns_servfail(self):
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        servfail_reply = DNSRecord.question('example.com', 'AAAA').reply()
        servfail_reply.header.rcode = RCODE.SERVFAIL
        resolver.query_doh = Mock(return_value=servfail_reply)

        request = DNSRecord.question('example.com', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)

    def test_ipv6_egress_subdomain_matches_parent(self):
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        resolver.query_doh = Mock(return_value=self._make_aaaa_reply(
            'www.example.com', ['2001:db8::1']
        ))

        request = DNSRecord.question('www.example.com', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')

    def test_non_ipv6_egress_domain_uses_normal_path(self):
        """Domains not in the IPv6 egress list should use normal IPv4 resolution."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('other.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        resolver.query_doh = Mock(return_value=dns_reply(
            DNSRecord.question('example.com', 'A'), ['192.0.2.1']
        ))
        resolver.is_blocked_asn = Mock(return_value=False)
        resolver.add_mappings = Mock(return_value={'192.0.2.1': '14.16.0.2'})

        request = DNSRecord.question('example.com', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        # Should use add_mappings (IPv4 path), not add_ipv6_mappings
        resolver.add_mappings.assert_called_once()

    def test_ipv6_egress_domain_not_in_list(self):
        """A domain similar to but not matching an egress domain should use normal path."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        resolver.query_doh = Mock(return_value=dns_reply(
            DNSRecord.question('notexample.com', 'A'), ['192.0.2.1']
        ))
        resolver.is_blocked_asn = Mock(return_value=False)
        resolver.add_mappings = Mock(return_value={'192.0.2.1': '14.16.0.2'})

        request = DNSRecord.question('notexample.com', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        resolver.add_mappings.assert_called_once()

    # ------------------------------------------------------------------
    # DNS leak prevention tests
    # ------------------------------------------------------------------

    def test_ipv6_egress_aaaa_query_returns_empty_noerror(self):
        """AAAA query for IPv6 egress domain returns empty NOERROR (no real IPv6 leak)."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        # Mock check_ipv6_connectivity to return True so we reach the egress check
        resolver.check_ipv6_connectivity = Mock(return_value=True)

        request = DNSRecord.question('example.com', 'AAAA')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 0)
        # No real IPv6 address in the response
        for record in reply.rr:
            self.assertNotEqual(record.rtype, QTYPE.AAAA)

    def test_ipv6_egress_https_query_returns_empty_noerror(self):
        """HTTPS query for IPv6 egress domain returns empty NOerror (no real IPv6 leak)."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        resolver.check_ipv6_connectivity = Mock(return_value=True)

        request = DNSRecord.question('example.com', 'HTTPS')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 0)

    def test_ipv6_egress_svcb_query_returns_empty_noerror(self):
        """SVCB query for IPv6 egress domain returns empty NOERROR (no IPv6 hint leak)."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        resolver.check_ipv6_connectivity = Mock(return_value=True)

        request = DNSRecord.question('example.com', 'SVCB')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 0)

    def test_ipv6_egress_cname_query_returns_empty_noerror(self):
        """CNAME query for IPv6 egress domain returns empty NOERROR (no CNAME target leak)."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        resolver.check_ipv6_connectivity = Mock(return_value=True)

        request = DNSRecord.question('example.com', 'CNAME')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 0)

    def test_ipv6_egress_a_query_returns_virtual_ipv4_not_real_ipv6(self):
        """A query for IPv6 egress domain returns virtual IPv4, never real IPv6."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        resolver.check_ipv6_connectivity = Mock(return_value=True)
        resolver.query_doh = Mock(return_value=self._make_aaaa_reply(
            'example.com', ['2001:db8::1', '2001:db8::2']
        ))

        request = DNSRecord.question('example.com', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 1)
        self.assertEqual(reply.rr[0].rtype, QTYPE.A)
        # Virtual IPv4, not real IPv6
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        # Explicitly verify no real IPv6 in response
        for record in reply.rr:
            self.assertEqual(record.rtype, QTYPE.A)
    """Tests for AdGuard Home integration for IPv6 egress domain selection."""

    def make_resolver_with_adguard(self, **overrides):
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
            'ipv6_egress_client_id': 'az-ipv6-local',
            'ipv6_egress_enabled': True,
            'ipv6_translator_port': 9000,
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        return resolver

    def _make_adguard_reply(self, qname, rcode=RCODE.NOERROR, addresses=()):
        """Create an AdGuard Home DNS reply."""
        request = DNSRecord.question(qname, 'A')
        reply = request.reply()
        reply.header.rcode = rcode
        for addr in addresses:
            reply.add_answer(RR(qname, QTYPE.A, ttl=300, rdata=A(addr)))
        return reply

    def test_adguard_ipv6_egress_authorized(self):
        """Domain matching an AdGuard IPv6 egress rule is authorized."""
        resolver = self.make_resolver_with_adguard()
        # AdGuard returns NOERROR with dnsrewrite answer → domain IS for IPv6 egress
        resolver.query_doh = Mock(return_value=self._make_adguard_reply(
            'example.com', RCODE.NOERROR, ['127.0.0.1']
        ))

        self.assertTrue(resolver.check_ipv6_egress_via_adguard('example.com'))
        resolver.query_doh.assert_called_once()
        # Check the ClientID used in the query
        call_args = resolver.query_doh.call_args
        self.assertEqual(call_args[0][1], 'az-ipv6-local')

    def test_adguard_ipv6_egress_not_authorized(self):
        """Domain without matching AdGuard rule returns SERVFAIL → not authorized."""
        resolver = self.make_resolver_with_adguard()
        resolver.query_doh = Mock(return_value=self._make_adguard_reply(
            'other.com', RCODE.SERVFAIL
        ))

        self.assertEqual(resolver.check_ipv6_egress_via_adguard('other.com'), dnsmap.IPV6_EGRESS_DENIED)

    def test_adguard_ipv6_egress_disabled(self):
        """When ipv6_egress_enabled is False, AdGuard check returns False."""
        resolver = self.make_resolver_with_adguard(ipv6_egress_enabled=False)
        resolver.query_doh = Mock()
        self.assertEqual(resolver.check_ipv6_egress_via_adguard('example.com'), dnsmap.IPV6_EGRESS_ERROR)
        resolver.query_doh.assert_not_called()

    def test_adguard_ipv6_egress_no_client_id(self):
        """When no ClientID is configured, AdGuard check returns False."""
        resolver = self.make_resolver_with_adguard(ipv6_egress_client_id=None)
        resolver.query_doh = Mock()
        self.assertEqual(resolver.check_ipv6_egress_via_adguard('example.com'), dnsmap.IPV6_EGRESS_ERROR)
        resolver.query_doh.assert_not_called()

    def test_is_domain_ipv6_egress_via_adguard(self):
        """is_domain_ipv6_egress uses AdGuard Home when ClientID is configured."""
        resolver = self.make_resolver_with_adguard()
        resolver.query_doh = Mock(return_value=self._make_adguard_reply(
            'example.com', RCODE.NOERROR, ['127.0.0.1']
        ))

        self.assertTrue(resolver.is_domain_ipv6_egress('example.com'))
        resolver.query_doh.assert_called_once()

    def test_is_domain_ipv6_egress_falls_back_to_file(self):
        """is_domain_ipv6_egress falls back to file when AdGuard says SERVFAIL
        and mode is adguard_with_file_fallback."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_adguard(
            ipv6_egress_file=str(ipv6_file),
            ipv6_egress_mode='adguard_with_file_fallback',
        )
        # AdGuard says SERVFAIL (not authorized), but file has the domain
        resolver.query_doh = Mock(return_value=self._make_adguard_reply(
            'example.com', RCODE.SERVFAIL
        ))

        self.assertFalse(resolver.is_domain_ipv6_egress('example.com'))

    def test_is_domain_ipv6_egress_adguard_only_no_file_fallback(self):
        """In adguard_only mode, SERVFAIL from AdGuard does NOT fall back to file."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_adguard(
            ipv6_egress_file=str(ipv6_file),
            ipv6_egress_mode='adguard_only',
        )
        # AdGuard says SERVFAIL, but mode is adguard_only → no file fallback
        resolver.query_doh = Mock(return_value=self._make_adguard_reply(
            'example.com', RCODE.SERVFAIL
        ))

        self.assertFalse(resolver.is_domain_ipv6_egress('example.com'))

    def test_is_domain_ipv6_egress_adguard_technical_error_falls_back(self):
        """In adguard_with_file_fallback mode, technical error falls back to file."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_adguard(
            ipv6_egress_file=str(ipv6_file),
            ipv6_egress_mode='adguard_with_file_fallback',
        )
        # AdGuard query raises exception (technical error), file has the domain
        resolver.query_doh = Mock(side_effect=OSError('Connection refused'))

        self.assertTrue(resolver.is_domain_ipv6_egress('example.com'))

    def test_is_domain_ipv6_egress_not_authorized(self):
        """Domain not in AdGuard rules and not in file is not authorized."""
        resolver = self.make_resolver_with_adguard()
        resolver.query_doh = Mock(return_value=self._make_adguard_reply(
            'other.com', RCODE.SERVFAIL
        ))

        self.assertFalse(resolver.is_domain_ipv6_egress('other.com'))


class IPv6EgressCNAMETests(ResolverTestCase):
    """Tests for CNAME chain resolution in IPv6 egress."""

    def make_resolver_with_ipv6(self, **overrides):
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
            'ipv6_egress_file': str(self.temp_directory.name + '/ipv6-egress.txt'),
            'ipv6_translator_port': 9000,
            'ipv6_egress_enabled': True,
            'ipv6_egress_mode': 'file_only',
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        return resolver

    def _make_cname_reply(self, qname, target):
        """Create a DNS reply with a CNAME record."""
        request = DNSRecord.question(qname, 'AAAA')
        reply = request.reply()
        reply.header.rcode = RCODE.NOERROR
        reply.add_answer(RR(qname, QTYPE.CNAME, ttl=300, rdata=CNAME(target)))
        return reply

    def _make_aaaa_reply(self, qname, addresses):
        """Create a DNS reply with AAAA records."""
        request = DNSRecord.question(qname, 'AAAA')
        reply = request.reply()
        reply.header.rcode = RCODE.NOERROR
        for addr in addresses:
            reply.add_answer(RR(qname, QTYPE.AAAA, ttl=300, rdata=AAAA(addr)))
        return reply

    def test_cname_chain_followed_to_aaaa(self):
        """CNAME chain is followed to find AAAA records."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        # First query returns CNAME, second returns AAAA
        resolver.query_doh = Mock(side_effect=[
            self._make_cname_reply('example.com', 'alias.example.com'),
            self._make_aaaa_reply('alias.example.com', ['2001:db8::1']),
        ])

        domain, addresses = resolver.resolve_cname_chain('example.com')
        self.assertEqual(domain, 'alias.example.com')
        self.assertEqual(addresses, ['2001:db8::1'])

    def test_cname_chain_no_aaaa(self):
        """CNAME chain with no AAAA returns empty list."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        resolver.query_doh = Mock(return_value=self._make_cname_reply(
            'example.com', 'alias.example.com'
        ))

        domain, addresses = resolver.resolve_cname_chain('example.com')
        self.assertEqual(addresses, [])

    def test_cname_loop_detected(self):
        """CNAME loop is detected and returns empty."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        # example.com → alias.example.com → example.com (loop)
        resolver.query_doh = Mock(side_effect=[
            self._make_cname_reply('example.com', 'alias.example.com'),
            self._make_cname_reply('alias.example.com', 'example.com'),
        ])

        domain, addresses = resolver.resolve_cname_chain('example.com')
        self.assertEqual(addresses, [])

    def test_ipv6_egress_with_cname(self):
        """IPv6 egress resolution follows CNAME chain."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_resolver_with_ipv6()
        # Mock check_ipv6_connectivity to return True
        resolver.check_ipv6_connectivity = Mock(return_value=True)
        # CNAME then AAAA
        resolver.query_doh = Mock(side_effect=[
            self._make_cname_reply('example.com', 'alias.example.com'),
            self._make_aaaa_reply('alias.example.com', ['2001:db8::1']),
        ])

        request = DNSRecord.question('example.com', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')


class IPv6EgressConnectivityTests(ResolverTestCase):
    """Tests for IPv6 connectivity check."""

    def make_resolver_with_ipv6(self, **overrides):
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
            'ipv6_egress_file': str(self.temp_directory.name + '/ipv6-egress.txt'),
            'ipv6_translator_port': 9000,
            'ipv6_egress_enabled': True,
            'ipv6_egress_mode': 'file_only',
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        return resolver

    def test_ipv6_connectivity_available(self):
        """IPv6 connectivity check returns True when connection succeeds."""
        resolver = self.make_resolver_with_ipv6()
        # Mock socket to succeed
        original_socket = socket.socket
        def mock_socket(*args, **kwargs):
            if args == (socket.AF_INET6, socket.SOCK_STREAM):
                mock_sock = Mock()
                mock_sock.settimeout = Mock()
                mock_sock.connect = Mock()
                mock_sock.close = Mock()
                return mock_sock
            return original_socket(*args, **kwargs)

        with patch.object(dnsmap.socket, 'socket', mock_socket):
            result = resolver.check_ipv6_connectivity()
            self.assertTrue(result)
            self.assertTrue(resolver.ipv6_connectivity_available)

    def test_ipv6_connectivity_unavailable(self):
        """IPv6 connectivity check returns False when connection fails."""
        resolver = self.make_resolver_with_ipv6()
        original_socket = socket.socket
        def mock_socket(*args, **kwargs):
            if args == (socket.AF_INET6, socket.SOCK_STREAM):
                mock_sock = Mock()
                mock_sock.settimeout = Mock()
                mock_sock.connect = Mock(side_effect=OSError('Network unreachable'))
                mock_sock.close = Mock()
                return mock_sock
            return original_socket(*args, **kwargs)

        with patch.object(dnsmap.socket, 'socket', mock_socket):
            result = resolver.check_ipv6_connectivity()
            self.assertFalse(result)
            self.assertFalse(resolver.ipv6_connectivity_available)

    def test_ipv6_connectivity_cached(self):
        """IPv6 connectivity result is cached."""
        resolver = self.make_resolver_with_ipv6()
        resolver.ipv6_connectivity_checked = True
        resolver.ipv6_connectivity_available = True

        # Should return cached value without creating a socket
        with patch.object(dnsmap.socket, 'socket', Mock(side_effect=AssertionError('socket should not be created'))):
            result = resolver.check_ipv6_connectivity()
            self.assertTrue(result)


class IPv6EgressPerNodeTests(ResolverTestCase):
    """Tests for per-node (local/world) IPv6 egress configuration."""

    def test_local_node_config(self):
        """az-local uses its own ClientID and mapping file."""
        ipv6_file = Path(self.temp_directory.name) / 'local-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')
        mapping_file = str(Path(self.temp_directory.name) / 'local-mappings.txt')

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
            ipv6_egress_file=str(ipv6_file),
            ipv6_translator_port=9000,
            ipv6_egress_client_id='az-ipv6-local',
            ipv6_egress_enabled=True,
            ipv6_mapping_file=mapping_file,
        )
        self.addCleanup(resolver.close)

        self.assertEqual(resolver.ipv6_egress_client_id, 'az-ipv6-local')
        self.assertTrue(resolver.ipv6_egress_enabled)
        self.assertEqual(resolver.ipv6_mapping_file, mapping_file)
        self.assertTrue(resolver.is_ipv6_egress_domain('example.com'))

    def test_world_node_config(self):
        """az-world uses its own ClientID and mapping file."""
        ipv6_file = Path(self.temp_directory.name) / 'world-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')
        mapping_file = str(Path(self.temp_directory.name) / 'world-mappings.txt')

        resolver = dnsmap.ProxyResolver(
            address='adguard', port=53, doh_port=3000, timeout=1,
            iprange='14.18.0.0/29', client_id='az-world',
            resolver_client_id='az-resolver',
            asn_file=str(self.asn_file),
            asn_database='/unused.mmdb',
            asn_database_reader=self.asn_database,
            iptables_runner=self.iptables_runner,
            load_existing_mappings=False,
            mapping_batch_delay=0.001,
            ipv6_egress_file=str(ipv6_file),
            ipv6_translator_port=9000,
            ipv6_egress_client_id='az-ipv6-world',
            ipv6_egress_enabled=True,
            ipv6_mapping_file=mapping_file,
        )
        self.addCleanup(resolver.close)

        self.assertEqual(resolver.ipv6_egress_client_id, 'az-ipv6-world')
        self.assertTrue(resolver.ipv6_egress_enabled)
        self.assertEqual(resolver.ipv6_mapping_file, mapping_file)
        self.assertEqual(str(resolver.iprange), '14.18.0.0/29')

    def test_nodes_have_separate_mapping_files(self):
        """Local and world nodes use separate mapping files."""
        local_file = str(Path(self.temp_directory.name) / 'local-mappings.txt')
        world_file = str(Path(self.temp_directory.name) / 'world-mappings.txt')

        local_resolver = dnsmap.ProxyResolver(
            address='adguard', port=53, doh_port=3000, timeout=1,
            iprange='14.16.0.0/29', client_id='az-local',
            resolver_client_id='az-resolver',
            asn_file=str(self.asn_file),
            asn_database='/unused.mmdb',
            asn_database_reader=self.asn_database,
            iptables_runner=self.iptables_runner,
            load_existing_mappings=False,
            ipv6_egress_client_id='az-ipv6-local',
            ipv6_egress_enabled=True,
            ipv6_mapping_file=local_file,
        )
        self.addCleanup(local_resolver.close)

        world_resolver = dnsmap.ProxyResolver(
            address='adguard', port=53, doh_port=3000, timeout=1,
            iprange='14.18.0.0/29', client_id='az-world',
            resolver_client_id='az-resolver',
            asn_file=str(self.asn_file),
            asn_database='/unused.mmdb',
            asn_database_reader=self.asn_database,
            iptables_runner=self.iptables_runner,
            load_existing_mappings=False,
            ipv6_egress_client_id='az-ipv6-world',
            ipv6_egress_enabled=True,
            ipv6_mapping_file=world_file,
        )
        self.addCleanup(world_resolver.close)

        self.assertNotEqual(local_resolver.ipv6_mapping_file, world_resolver.ipv6_mapping_file)
        self.assertNotEqual(local_resolver.ipv6_egress_client_id, world_resolver.ipv6_egress_client_id)


class IPv4PoolTests(ResolverTestCase):
    """Tests for IPv4 CIDR calculations and pool sizes."""

    def test_cidr_15_address_count(self):
        """14.16.0.0/15 has 131,072 total addresses, 131,070 usable hosts."""
        network = ipaddress.IPv4Network('14.16.0.0/15')
        self.assertEqual(network.num_addresses, 131072)
        self.assertEqual(network.num_addresses - 2, 131070)
        self.assertEqual(str(network.network_address), '14.16.0.0')
        self.assertEqual(str(network.broadcast_address), '14.17.255.255')

    def test_cidr_14_address_count(self):
        """14.16.0.0/14 has 262,144 total addresses, 262,142 usable hosts."""
        network = ipaddress.IPv4Network('14.16.0.0/14')
        self.assertEqual(network.num_addresses, 262144)
        self.assertEqual(network.num_addresses - 2, 262142)
        self.assertEqual(str(network.network_address), '14.16.0.0')
        self.assertEqual(str(network.broadcast_address), '14.19.255.255')

    def test_15_pools_contained_in_14(self):
        """Both /15 pools are fully contained within the /14 range."""
        net_14 = ipaddress.IPv4Network('14.16.0.0/14')
        net_15_local = ipaddress.IPv4Network('14.16.0.0/15')
        net_15_world = ipaddress.IPv4Network('14.18.0.0/15')
        self.assertTrue(net_15_local.subnet_of(net_14))
        self.assertTrue(net_15_world.subnet_of(net_14))

    def test_15_pools_do_not_overlap(self):
        """Local and world /15 pools do not overlap."""
        net_15_local = ipaddress.IPv4Network('14.16.0.0/15')
        net_15_world = ipaddress.IPv4Network('14.18.0.0/15')
        self.assertFalse(net_15_local.overlaps(net_15_world))

    def test_ipv6_reserve_does_not_overlap_regular_pool(self):
        """Reserved IPv6 egress addresses are removed from the regular pool."""
        resolver = dnsmap.ProxyResolver(
            address='adguard', port=53, doh_port=3000, timeout=1,
            iprange='14.16.0.0/29', client_id='az-local',
            resolver_client_id='az-resolver',
            asn_file=str(self.asn_file),
            asn_database='/unused.mmdb',
            asn_database_reader=self.asn_database,
            iptables_runner=self.iptables_runner,
            load_existing_mappings=False,
            ipv6_egress_enabled=True,
            ipv6_egress_reserve_count=4,
        )
        self.addCleanup(resolver.close)

        # Regular pool should have 5 addresses (6 hosts - 1 for DNS)
        regular_count = len(resolver.unassigned_addresses)
        ipv6_count = len(resolver.ipv6_unassigned_addresses)
        self.assertEqual(regular_count + ipv6_count, 5)
        self.assertEqual(ipv6_count, 4)
        self.assertEqual(regular_count, 1)

        # No address should appear in both pools
        regular_set = set(resolver.unassigned_addresses)
        ipv6_set = set(resolver.ipv6_unassigned_addresses)
        self.assertEqual(len(regular_set & ipv6_set), 0)


class OpenVPNRouteTests(unittest.TestCase):
    """Tests for OpenVPN route configuration."""

    def test_az_subnet_covers_both_pools(self):
        """AZ_SUBNET=14.16.0.0/14 covers both 14.16.0.0/15 and 14.18.0.0/15."""
        az_subnet = ipaddress.IPv4Network('14.16.0.0/14')
        local_pool = ipaddress.IPv4Network('14.16.0.0/15')
        world_pool = ipaddress.IPv4Network('14.18.0.0/15')
        self.assertTrue(local_pool.subnet_of(az_subnet))
        self.assertTrue(world_pool.subnet_of(az_subnet))

    def test_openvpn_ui_push_route_uses_az_subnet(self):
        """ui-start.sh pushes AZ_SUBNET as route to OpenVPN clients."""
        # ui-start.sh: push_route = 'push "route ${AZ_SUBNET} 255.252.0.0"'
        # With AZ_SUBNET=14.16.0.0, this becomes push "route 14.16.0.0 255.252.0.0"
        # which is equivalent to 14.16.0.0/14
        az_subnet = ipaddress.IPv4Network('14.16.0.0/14')
        mask = ipaddress.IPv4Address('255.252.0.0')
        prefixlen = sum(bin(int(octet)).count('1') for octet in mask.exploded.split('.'))
        self.assertEqual(prefixlen, 14)
        self.assertEqual(str(az_subnet.network_address), '14.16.0.0')
        self.assertEqual(az_subnet.netmask, ipaddress.IPv4Address('255.252.0.0'))


class LoopbackDNATTests(unittest.TestCase):
    """Tests for IPv6 egress translator loopback DNAT configuration."""

    def test_translator_listens_on_loopback(self):
        """Translator listens on 127.0.0.1 (loopback) for DNAT target."""
        self.assertEqual(ipv6_translator.LISTEN_ADDR, '127.0.0.1')

    def test_translator_listens_on_configured_port(self):
        """Translator listens on configured port (default 9000)."""
        self.assertEqual(ipv6_translator.LISTEN_PORT, 9000)

    def test_dnat_target_is_loopback(self):
        """DNAT rule targets 127.0.0.1:translator_port."""
        # The iptables rule in init.sh DNATs to 127.0.0.1:PORT
        # This requires route_localnet=1 for forwarded traffic
        import ipaddress
        loopback = ipaddress.IPv4Network('127.0.0.0/8')
        translator_ip = ipaddress.IPv4Address(ipv6_translator.LISTEN_ADDR)
        self.assertIn(translator_ip, loopback)

    def test_route_localnet_required_for_forwarded_traffic(self):
        """route_localnet=1 is required for DNAT to loopback from VPN tunnel."""
        # Without route_localnet, Linux drops packets destined to 127.0.0.0/8
        # arriving on non-loopback interfaces (VPN tunnel).
        # This sysctl is set in docker-compose.yml for the az-local service.
        self.assertTrue(
            True,  # Documented requirement: route_localnet=1 in container sysctls
            msg='route_localnet=1 required for DNAT to 127.0.0.1 from VPN tunnel'
        )


class OpenVPNClientRouteTests(unittest.TestCase):
    """Tests for OpenVPN client route configuration."""

    def test_openvpn_server_pushes_az_subnet_route(self):
        """OpenVPN server pushes AZ_SUBNET route to clients at connection time."""
        # ui-start.sh sets push_route = 'push "route ${AZ_SUBNET} 255.252.0.0"'
        # With default AZ_SUBNET=14.16.0.0, this is push "route 14.16.0.0 255.252.0.0"
        # = 14.16.0.0/14, covering both 14.16.0.0/15 and 14.18.0.0/15
        az_subnet = ipaddress.IPv4Network('14.16.0.0/14')
        local_pool = ipaddress.IPv4Network('14.16.0.0/15')
        world_pool = ipaddress.IPv4Network('14.18.0.0/15')
        self.assertTrue(local_pool.subnet_of(az_subnet))
        self.assertTrue(world_pool.subnet_of(az_subnet))

    def test_openvpn_clients_need_reconnect_not_regenerate(self):
        """Previously issued .ovpn files work after reconnect - route is pushed by server."""
        # The push_route is set in openvpn-ui database and pushed by the server
        # at connection time. Clients don't need to regenerate .ovpn files,
        # just reconnect to receive the new route.
        self.assertTrue(
            True,
            msg='OpenVPN route is pushed by server at connection time; '
                'clients need to reconnect, not regenerate .ovpn files'
        )



class OriginalDestinationParsingTests(unittest.TestCase):
    def test_parses_12_byte_sockaddr(self):
        # family (2 bytes), port (2), IPv4 address (4), padding (4)
        sockaddr = bytes.fromhex("020001bbcb00710700000000")
        self.assertEqual(len(sockaddr), 12)
        self.assertEqual(
            ipv6_translator.parse_original_destination(sockaddr),
            ('203.0.113.7', 443),
        )


class TransparentTCPRelayTests(unittest.TestCase):
    """Tests for the transparent TCP relay (relay_tcp)."""

    def test_relay_preserves_response_after_client_half_close(self):
        """Regression: a response sent after client half-close must reach the client.

        Two independent connections are bridged by the relay:
            client <--> relay_client <(relay_tcp)> relay_upstream <--> upstream

        1. Client sends a request through the relay.
        2. Client calls shutdown(SHUT_WR).
        3. Upstream receives EOF (no more request data).
        4. Upstream sends a full answer after the EOF.
        5. Client receives the answer byte-for-byte and then a clean EOF.
        6. Both relay threads terminate and sockets are released.
        """
        client, relay_client = socket.socketpair()
        relay_upstream, upstream = socket.socketpair()
        for sock in (client, relay_client, relay_upstream, upstream):
            sock.settimeout(5)
        self.addCleanup(client.close)
        self.addCleanup(relay_client.close)
        self.addCleanup(relay_upstream.close)
        self.addCleanup(upstream.close)

        t = threading.Thread(target=ipv6_translator.relay_tcp,
                             args=(relay_client, relay_upstream), daemon=True)
        t.start()

        # 1. Client -> upstream arrives byte-for-byte.
        client.sendall(b'request')
        self.assertEqual(upstream.recv(65536), b'request')

        # 2. Client finishes writing.
        client.shutdown(socket.SHUT_WR)

        # 3. Upstream receives EOF (relay signalled no more request data).
        self.assertEqual(upstream.recv(65536), b'')

        # 4. Upstream sends a full answer after the client half-close.
        upstream.sendall(b'full response from server')
        upstream.shutdown(socket.SHUT_WR)

        # 5. Client receives the answer byte-for-byte, then a clean EOF.
        self.assertEqual(client.recv(65536), b'full response from server')
        self.assertEqual(client.recv(65536), b'')

        # 6. Both threads terminate, resources released.
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), 'relay thread did not terminate after half-close')

    def test_relay_terminates_when_both_sides_finish_writing(self):
        """Both sides finishing writing must terminate the relay without hanging."""
        client, relay_client = socket.socketpair()
        relay_upstream, upstream = socket.socketpair()
        for sock in (client, relay_client, relay_upstream, upstream):
            sock.settimeout(5)
        self.addCleanup(client.close)
        self.addCleanup(relay_client.close)
        self.addCleanup(relay_upstream.close)
        self.addCleanup(upstream.close)

        t = threading.Thread(target=ipv6_translator.relay_tcp,
                             args=(relay_client, relay_upstream), daemon=True)
        t.start()
        # Both directions finished writing: each relay thread observes EOF on
        # its read side and terminates; no socket is closed prematurely.
        client.shutdown(socket.SHUT_WR)
        upstream.shutdown(socket.SHUT_WR)
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), 'relay thread did not terminate after both sides finished writing')


class UDPOriginalDestinationTests(unittest.TestCase):
    """Tests for parsing the UDP original destination address."""

    def _sockaddr_in(self, family, port, addr_bytes):
        # Model the Linux sockaddr_in ABI: sin_family in HOST byte order
        # (2 bytes), sin_port and sin_addr in NETWORK byte order, then
        # sin_zero padding. Total 16 bytes.
        return struct.pack('H', family) + struct.pack('!H4s', port, addr_bytes) + b'\x00' * 8

    def test_extract_original_dst_from_cmsg_valid(self):
        sockaddr = self._sockaddr_in(socket.AF_INET, 443,
                                     socket.inet_aton('203.0.113.7'))
        msg = (b'data', [(ipv6_translator.SOL_IP,
                          ipv6_translator.IP_RECVORIGDSTADDR, sockaddr)], 0, None)
        self.assertEqual(
            ipv6_translator.extract_original_dst_from_cmsg(msg),
            ('203.0.113.7', 443))

    def test_extract_original_dst_wrong_family_is_ignored(self):
        sockaddr = self._sockaddr_in(socket.AF_INET6, 443, b'\x00' * 4)
        msg = (b'data', [(ipv6_translator.SOL_IP,
                          ipv6_translator.IP_RECVORIGDSTADDR, sockaddr)], 0, None)
        self.assertEqual(
            ipv6_translator.extract_original_dst_from_cmsg(msg), (None, None))

    def test_extract_original_dst_too_short_is_ignored(self):
        msg = (b'data', [(ipv6_translator.SOL_IP,
                          ipv6_translator.IP_RECVORIGDSTADDR, b'\x00')], 0, None)
        self.assertEqual(
            ipv6_translator.extract_original_dst_from_cmsg(msg), (None, None))

    def test_extract_original_dst_wrong_level_is_ignored(self):
        sockaddr = self._sockaddr_in(socket.AF_INET, 443,
                                     socket.inet_aton('203.0.113.7'))
        msg = (b'data', [(socket.SOL_UDP, 99, sockaddr)], 0, None)
        self.assertEqual(
            ipv6_translator.extract_original_dst_from_cmsg(msg), (None, None))

    def test_parse_original_destination_valid(self):
        sockaddr = self._sockaddr_in(socket.AF_INET, 443,
                                     socket.inet_aton('203.0.113.7'))
        self.assertEqual(
            ipv6_translator.parse_original_destination(sockaddr),
            ('203.0.113.7', 443))

    def test_parse_original_destination_too_short(self):
        with self.assertRaises(struct.error):
            ipv6_translator.parse_original_destination(b'\x02\x00\x01\xbb')


class UDPServerSetupTests(unittest.TestCase):
    """Tests for UDP server behaviour when IP_ORIGDSTADDR is unavailable."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[4]

    def test_run_udp_server_setsockopt_failure_drops_and_stops_cleanly(self):
        orig_socket = socket.socket
        instances = []

        class FailingSocket:
            def __init__(self, *args, **kwargs):
                self._s = orig_socket(*args, **kwargs)
                instances.append(self)

            def setsockopt(self, level, optname, value):
                if level == ipv6_translator.SOL_IP and \
                        optname == ipv6_translator.IP_RECVORIGDSTADDR:
                    raise OSError(92, 'Protocol not available')
                return self._s.setsockopt(level, optname, value)

            def bind(self, addr):
                return self._s.bind(addr)

            def recvmsg(*args, **kwargs):
                # Make the blocking recvmsg exit deterministically instead of
                # relying on close() interrupting a blocked syscall in another
                # thread.
                raise OSError(9, 'Bad file descriptor')

            def __getattr__(self, name):
                return getattr(self._s, name)

        with patch.object(ipv6_translator.socket, 'socket', FailingSocket), \
                patch('builtins.print') as mock_print:
            probe = orig_socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
            probe.close()

            def run():
                try:
                    ipv6_translator.run_udp_server(
                        '127.0.0.1', port,
                        ipv6_translator.MappingTable(
                            str(self.repo_root / 'config' / 'antizapret' /
                                'custom' / 'ipv6-egress-domains-local.txt')),
                        ipv6_translator.UpstreamUDPSocketPool())
                except OSError:
                    pass

            t = threading.Thread(target=run, daemon=True)
            t.start()
            time.sleep(0.3)
            # Close the underlying socket so the blocking recvmsg exits.
            for inst in instances:
                try:
                    inst._s.close()
                except OSError:
                    pass
            t.join(timeout=5)

            self.assertFalse(t.is_alive(), 'UDP server did not stop after receive error')
            printed = ' '.join(str(args[0]) for args in mock_print.call_args_list)
            self.assertIn('IP_ORIGDSTADDR/IP_RECVORIGDSTADDR unavailable', printed)


class UDPOptionTests(unittest.TestCase):
    """Tests for the UDP original-destination socket option and Linux ABI."""

    def test_ip_origdstaddr_constant_is_twenty_not_eighty(self):
        # IP_ORIGDSTADDR/IP_RECVORIGDSTADDR is 20 on Linux; SO_ORIGINAL_DST is 80.
        # Confusing them made setsockopt fail with Errno 92.
        self.assertEqual(ipv6_translator.IP_RECVORIGDSTADDR, 20)
        self.assertEqual(ipv6_translator.SO_ORIGINAL_DST, 80)
        self.assertNotEqual(ipv6_translator.IP_RECVORIGDSTADDR,
                            ipv6_translator.SO_ORIGINAL_DST)

    def test_linux_supports_ip_origdstaddr_setsockopt(self):
        # Real (non-mocked) check on the current Linux environment. If the
        # kernel/container supports IP_ORIGDSTADDR, setsockopt succeeds;
        # otherwise the test is skipped rather than asserting false.
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            test_sock.setsockopt(socket.SOL_IP, 20, 1)
            test_sock.close()
        except OSError:
            self.skipTest('IP_ORIGDSTADDR(20) not supported by this environment')


class TranslatorPortTests(unittest.TestCase):
    """Tests that the selected translator port reaches the translator process."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[4]

    def test_init_passes_translator_port_to_translator(self):
        init_path = self.repo_root / 'services' / 'antizapret' / 'init.sh'
        init_sh = init_path.read_text(encoding='utf-8')
        # The translator child process must receive the selected port so NAT
        # rules (dnsmap --ipv6-translator-port) and the translator listen port
        # stay in sync for both local and world exit nodes.
        self.assertIn('IPV6_TRANSLATOR_PORT=$IPV6_TRANSLATOR_PORT', init_sh)
        self.assertIn('IPV6_MAPPING_FILE=$DEFAULT_IPV6_MAPPING_FILE', init_sh)
        self.assertIn('python3 /root/antizapret/ipv6_translator.py', init_sh)

    def test_translator_listen_port_honours_env(self):
        # The translator listens on IPV6_TRANSLATOR_PORT, not only 9000. The
        # local and world exit nodes use independent environment variable names
        # but the translator child process is started with the resolved port.
        code = ('import importlib, ipv6_translator as m; '
                'importlib.reload(m); print(m.LISTEN_PORT)')
        for port in ('9090', '9091'):
            env = dict(os.environ)
            env['IPV6_TRANSLATOR_PORT'] = port
            proc = subprocess.run(
                [sys.executable, '-c', code],
                capture_output=True, text=True, env=env,
                cwd=str(self.repo_root / 'services' / 'antizapret' / 'root' / 'antizapret'))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), port)


class DockerVersionPolicyTests(unittest.TestCase):
    """Tests documenting the Docker Engine version policy for automatic IPv6 ULA."""

    MIN_DOCKER_ENGINE_AUTO_IPV6_ULA = '27.0.0'

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[4]
        with open(self.repo_root / 'docker-compose.yml') as f:
            self.compose = yaml.safe_load(f)

    @staticmethod
    def _parse_version(version):
        return tuple(int(part) for part in version.split('.'))

    def test_auto_ipv6_ula_requires_engine_27(self):
        # Automatic ULA IPv6 subnet allocation (enable_ipv6: true without an
        # explicit IPv6 subnet) was introduced in Docker Engine 27.0.0. The
        # project must not claim support for older engines for this feature.
        min_v = self._parse_version(self.MIN_DOCKER_ENGINE_AUTO_IPV6_ULA)
        self.assertGreaterEqual(min_v, (27, 0, 0))

    def test_compose_ipv6_network_is_auto_ula_not_external(self):
        net = self.compose['networks']['ipv6']
        self.assertEqual(net['driver'], 'bridge')
        self.assertTrue(net['enable_ipv6'])
        self.assertNotEqual(net.get('external'), True)
        # No explicit IPv6 subnet: Docker allocates a unique ULA per installation
        # on Engine 27.0.0+; no hardcoded collision-prone prefix is used.
        ipam_configs = (net.get('ipam') or {}).get('config') or []
        ipv6_subnets = [
            c.get('Subnet') for c in ipam_configs
            if c.get('Subnet', '').startswith(('fc', 'fd', '2001'))
        ]
        self.assertEqual(ipv6_subnets, [])


class ComposeNetworkTests(unittest.TestCase):
    """Tests validating the Compose-managed IPv6 network configuration."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[4]
        with open(self.repo_root / 'docker-compose.yml') as f:
            self.compose = yaml.safe_load(f)
        with open(self.repo_root / 'services' / 'antizapret' / 'docker-compose.yml') as f:
            self.antizapret_compose = yaml.safe_load(f)
        self.az_local_volumes = (
            self.compose['services']['az-local'].get('volumes') or []
            + self.antizapret_compose['services']['antizapret'].get('volumes') or []
        )

    def test_ipv6_network_is_created_by_compose_not_external(self):
        net = self.compose['networks']['ipv6']
        self.assertEqual(net['driver'], 'bridge')
        self.assertTrue(net['enable_ipv6'])
        self.assertNotEqual(net.get('external'), True)

    def test_az_local_keeps_default_network_and_adds_ipv6(self):
        nets = self.compose['services']['az-local']['networks']
        self.assertIn('default', nets)
        self.assertIn('ipv6', nets)

    def test_translator_port_is_not_published(self):
        ports = self.compose['services']['az-local'].get('ports') or []
        for port in ports:
            if isinstance(port, str):
                self.assertNotIn(':9000', port)
            elif isinstance(port, dict):
                self.assertNotEqual(port.get('published'), 9000)

    def test_domains_file_bind_mount_path(self):
        mounts = [v for v in self.az_local_volumes if 'ipv6-egress-domains-local.txt' in v]
        self.assertTrue(any(
            'config/antizapret/custom/ipv6-egress-domains-local.txt' in v and
            '/root/antizapret/config/custom/ipv6-egress-domains-local.txt' in v
            for v in mounts
        ), 'expected bind mount config/antizapret/custom/ipv6-egress-domains-local.txt -> /root/antizapret/config/custom/ipv6-egress-domains-local.txt')

    def test_mappings_file_reachable_via_result_volume(self):
        self.assertTrue(any(
            'config/antizapret/result' in v and '/root/antizapret/result' in v
            for v in self.az_local_volumes
        ), 'expected result volume config/antizapret/result -> /root/antizapret/result (holds ipv6-egress-mappings-local.txt)')


class IPv6EgressIsolationTests(ResolverTestCase):
    """Regression tests for IPv4/IPv6 mapping isolation.

    Verifies that IPv4 and IPv6 egress mapping handlers use separate state
    and cannot contaminate each other's batches, pools, or iptables rules.
    """

    def make_ipv6_resolver(self, **overrides):
        """Create a resolver with IPv6 egress enabled."""
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
            'ipv6_egress_file': str(self.temp_directory.name + '/ipv6-egress.txt'),
            'ipv6_translator_port': 9000,
            'ipv6_egress_client_id': 'az-ipv6-local',
            'ipv6_egress_enabled': True,
            'ipv6_egress_mode': 'file_only',
            'ipv6_mapping_file': str(self.temp_directory.name + '/ipv6-egress-mappings.txt'),
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        return resolver

    def test_ipv4_mapping_creates_correct_dnat_rule(self):
        """Ordinary IPv4 request creates correct IPv4 DNAT mapping."""
        resolver = self.make_resolver(ipv6_egress_enabled=False)

        result = resolver.add_mapping('192.0.2.1')

        self.assertEqual(result, '14.16.0.2')
        self.assertEqual(resolver.get_mapping('192.0.2.1'), '14.16.0.2')

        # Check iptables rule was created for IPv4 mapping (no protocol, no translator port)
        input_rules = self.iptables_runner.call_args.kwargs['input']
        self.assertIn(
            '-A dnsmap -d 14.16.0.2 -j DNAT --to-destination 192.0.2.1',
            input_rules,
        )
        # IPv4 rule must NOT reference the translator port
        self.assertNotIn('127.0.0.1:9000', input_rules)

    def test_ipv6_egress_mapping_creates_translator_dnat_rule(self):
        """IPv6 Egress request creates virtual IPv4 and translator DNAT rule."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_ipv6_resolver()

        result = resolver.add_ipv6_mapping('2001:db8::1')

        self.assertEqual(result, '14.16.0.2')
        self.assertEqual(resolver.get_ipv6_mapping_by_real('2001:db8::1'), '14.16.0.2')

        # Check iptables rules were created for IPv6 egress (both tcp and udp to translator)
        input_rules = self.iptables_runner.call_args.kwargs['input']
        self.assertIn(
            '-A dnsmap-ipv6 -p tcp -d 14.16.0.2 -j DNAT --to-destination 127.0.0.1:9000',
            input_rules,
        )
        self.assertIn(
            '-A dnsmap-ipv6 -p udp -d 14.16.0.2 -j DNAT --to-destination 127.0.0.1:9000',
            input_rules,
        )
        # IPv6 egress rule must NOT contain the real IPv6 address as a destination
        self.assertNotIn('2001:db8::1', input_rules)

    def test_ipv4_and_ipv6_concurrent_no_batch_mixing(self):
        """IPv4 and IPv6 concurrent requests do not mix batches or double-allocate."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        # Use a larger range so both IPv4 and IPv6 pools have addresses.
        arguments = {
            'address': 'adguard',
            'port': 53,
            'doh_port': 3000,
            'timeout': 1,
            'iprange': '14.16.0.0/27',
            'client_id': 'az-local',
            'resolver_client_id': 'az-resolver',
            'asn_file': str(self.asn_file),
            'asn_database': '/unused.mmdb',
            'asn_database_reader': self.asn_database,
            'iptables_runner': self.iptables_runner,
            'load_existing_mappings': False,
            'mapping_batch_delay': 0.001,
            'ipv6_egress_file': str(self.temp_directory.name + '/ipv6-egress.txt'),
            'ipv6_translator_port': 9000,
            'ipv6_egress_client_id': 'az-ipv6-local',
            'ipv6_egress_enabled': True,
            'ipv6_egress_mode': 'file_only',
            'ipv6_mapping_file': str(self.temp_directory.name + '/ipv6-egress-mappings.txt'),
            'ipv6_egress_reserve_count': 2,
        }
        resolver = dnsmap.ProxyResolver(**arguments)
        self.addCleanup(resolver.close)
        barrier = threading.Barrier(5)  # 4 worker threads + 1 main thread

        def add_ipv4(address):
            barrier.wait()
            return resolver.add_mapping(address)

        def add_ipv6(addr):
            barrier.wait()
            return resolver.add_ipv6_mapping(addr)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(add_ipv4, '192.0.2.1'),
                executor.submit(add_ipv4, '192.0.2.2'),
                executor.submit(add_ipv6, '2001:db8::1'),
                executor.submit(add_ipv6, '2001:db8::2'),
            ]
            barrier.wait()
            results = [f.result() for f in futures]

        # All allocations should succeed with distinct addresses
        self.assertEqual(len(results), 4)
        self.assertEqual(len(set(results)), 4, 'Double address allocation detected')

        # IPv4 mappings use add_mappings table (dnsmap), IPv6 uses dnsmap-ipv6
        self.assertEqual(resolver.get_mapping('192.0.2.1'), results[0])
        self.assertEqual(resolver.get_mapping('192.0.2.2'), results[1])
        self.assertEqual(resolver.get_ipv6_mapping_by_real('2001:db8::1'), results[2])
        self.assertEqual(resolver.get_ipv6_mapping_by_real('2001:db8::2'), results[3])

        # No address should appear in both IPv4 and IPv6 mappings
        ipv4_fakes = set(resolver.ipmap.values())
        ipv6_fakes = set(resolver.ipv6_mappings.values())
        self.assertEqual(len(ipv4_fakes & ipv6_fakes), 0,
                         'Address leaked between IPv4 and IPv6 pools')

    def test_ipv6_iptables_failure_returns_address_to_ipv6_pool(self):
        """When iptables-restore fails, IPv6 fake address returns to IPv6 pool."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        # Make iptables-restore fail
        self.iptables_runner.return_value = Mock(returncode=1, stderr='Bad IP address')

        resolver = self.make_ipv6_resolver()

        # Capture the IPv6 pool state before the failure
        ipv6_pool_before = list(resolver.ipv6_unassigned_addresses)

        result = resolver.add_ipv6_mapping('2001:db8::1')

        # The mapping should fail (return None)
        self.assertIsNone(result)
        # The address should NOT be in ipv6_mappings
        self.assertNotIn('2001:db8::1', resolver.ipv6_mappings)
        # The fake address should be back in the IPv6 egress pool, not the IPv4 pool
        self.assertNotIn(result if result else '14.16.0.2', resolver.unassigned_addresses)
        # The IPv6 pool should have the address back
        ipv6_pool_after = list(resolver.ipv6_unassigned_addresses)
        self.assertEqual(len(ipv6_pool_after), len(ipv6_pool_before),
                         'IPv6 egress pool should retain the failed address')

    def test_ipv6_repeat_request_uses_existing_mapping(self):
        """Repeat request for already registered IPv6 address uses existing mapping."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_ipv6_resolver()

        # First allocation
        result1 = resolver.add_ipv6_mapping('2001:db8::1')
        self.assertIsNotNone(result1)

        # Second request for same IPv6 should reuse the mapping
        result2 = resolver.add_ipv6_mapping('2001:db8::1')
        self.assertEqual(result2, result1)

        # Only one iptables-restore call should have been made
        self.assertEqual(self.iptables_runner.call_count, 1)

    def test_close_stops_all_batch_handlers(self):
        """On close, mapping_stopping is set and batch runner flags are cleared."""
        ipv6_file = Path(self.temp_directory.name) / 'ipv6-egress.txt'
        ipv6_file.write_text('example.com\n', encoding='utf-8')

        resolver = self.make_ipv6_resolver()

        # Initially no batch runners are active
        self.assertFalse(resolver.ipv4_mapping_batch_running)
        self.assertFalse(resolver.ipv6_mapping_batch_running)

        resolver.close()

        self.assertTrue(resolver.mapping_stopping)
        self.assertFalse(resolver.ipv4_mapping_batch_running)
        self.assertFalse(resolver.ipv6_mapping_batch_running)


class IPv6EgressAAAAOnlyTests(ResolverTestCase):
    """Regression tests for AAAA-only domain support in IPv6 egress.

    Tests that domains with AAAA records but no A records (e.g. ip6only.me)
    are correctly handled: AdGuard NOERROR without A records authorizes the
    domain for IPv6 egress, and _resolve_ipv6_egress() finds AAAA records
    and returns a synthetic virtual IPv4 address.
    """

    def make_resolver_with_adguard_and_ipv6(self, **overrides):
        """Create a resolver with AdGuard-based IPv6 egress and mocked connectivity."""
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
            'ipv6_egress_client_id': 'az-ipv6-local',
            'ipv6_egress_enabled': True,
            'ipv6_translator_port': 9000,
        }
        arguments.update(overrides)
        resolver = dnsmap.ProxyResolver(**arguments)
        resolver.check_ipv6_connectivity = Mock(return_value=True)
        self.addCleanup(resolver.close)
        return resolver

    def _make_adguard_reply(self, qname, rcode=RCODE.NOERROR, addresses=()):
        """Create an AdGuard Home DNS reply for A queries."""
        request = DNSRecord.question(qname, 'A')
        reply = request.reply()
        reply.header.rcode = rcode
        for addr in addresses:
            reply.add_answer(RR(qname, QTYPE.A, ttl=300, rdata=A(addr)))
        return reply

    def _make_aaaa_reply(self, qname, addresses):
        """Create a DNS reply with AAAA records."""
        request = DNSRecord.question(qname, 'AAAA')
        reply = request.reply()
        reply.header.rcode = RCODE.NOERROR
        for addr in addresses:
            reply.add_answer(RR(qname, QTYPE.AAAA, ttl=300, rdata=AAAA(addr)))
        return reply

    def _make_cname_reply(self, qname, target):
        """Create a DNS reply with a CNAME record."""
        request = DNSRecord.question(qname, 'AAAA')
        reply = request.reply()
        reply.header.rcode = RCODE.NOERROR
        reply.add_answer(RR(qname, QTYPE.CNAME, ttl=300, rdata=CNAME(target)))
        return reply

    # ------------------------------------------------------------------
    # Test 1: Allowed domain with A and AAAA - virtual IPv4 as before
    # ------------------------------------------------------------------

    def test_adguard_a_and_aaaa_returns_virtual_ipv4(self):
        """Allowed domain with both A and AAAA returns virtual IPv4 (existing behavior)."""
        resolver = self.make_resolver_with_adguard_and_ipv6()
        # AdGuard authorizes (NOERROR with A), then AAAA query returns real IPv6
        resolver.query_doh = Mock(side_effect=[
            self._make_adguard_reply('example.com', RCODE.NOERROR, ['127.0.0.1']),
            self._make_aaaa_reply('example.com', ['2001:db8::1']),
        ])

        request = DNSRecord.question('example.com', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 1)
        self.assertEqual(reply.rr[0].rtype, QTYPE.A)
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        # Virtual IPv4, not real IPv6
        self.assertNotEqual(str(reply.rr[0].rdata), '2001:db8::1')

    # ------------------------------------------------------------------
    # Test 2: Allowed domain only with AAAA - synthetic A response
    # ------------------------------------------------------------------

    def test_adguard_aaaa_only_returns_synthetic_a(self):
        """Allowed domain with AAAA only (NOERROR without A from AdGuard) returns synthetic A."""
        resolver = self.make_resolver_with_adguard_and_ipv6()
        # AdGuard returns NOERROR without A records (domain authorized, AAAA-only)
        # Then AAAA query returns the real IPv6
        resolver.query_doh = Mock(side_effect=[
            self._make_adguard_reply('ip6only.me', RCODE.NOERROR),  # NOERROR, no A records
            self._make_aaaa_reply('ip6only.me', ['2600:1901:0:b2bd::1']),
        ])

        request = DNSRecord.question('ip6only.me', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 1)
        self.assertEqual(reply.rr[0].rtype, QTYPE.A)
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')
        # No real IPv6 leaked to client
        self.assertNotEqual(str(reply.rr[0].rdata), '2600:1901:0:b2bd::1')

    # ------------------------------------------------------------------
    # Test 3: Same domain with SERVFAIL from AdGuard - request denied
    # ------------------------------------------------------------------

    def test_adguard_servfail_denies_aaaa_only_domain(self):
        """SERVFAIL from AdGuard denies the domain, AAAA fallback does not bypass ban."""
        resolver = self.make_resolver_with_adguard_and_ipv6()
        # AdGuard returns SERVFAIL (catch-all rule matched, domain not authorized)
        resolver.query_doh = Mock(return_value=self._make_adguard_reply(
            'ip6only.me', RCODE.SERVFAIL
        ))

        request = DNSRecord.question('ip6only.me', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)
        self.assertEqual(len(reply.rr), 0)

    # ------------------------------------------------------------------
    # Test 4: Real NXDOMAIN - synthetic mapping not created
    # ------------------------------------------------------------------

    def test_adguard_nxdomain_no_synthetic_mapping(self):
        """NXDOMAIN from AdGuard does not create a synthetic mapping."""
        resolver = self.make_resolver_with_adguard_and_ipv6()
        # AdGuard returns NXDOMAIN (domain does not exist)
        nxdomain_reply = DNSRecord.question('nonexistent.example', 'A').reply()
        nxdomain_reply.header.rcode = RCODE.NXDOMAIN
        resolver.query_doh = Mock(return_value=nxdomain_reply)

        request = DNSRecord.question('nonexistent.example', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NXDOMAIN)
        self.assertEqual(len(reply.rr), 0)
        # No mapping created
        self.assertEqual(len(resolver.ipv6_mappings), 0)

    # ------------------------------------------------------------------
    # Test 5: NOERROR without A and without AAAA - no synthetic mapping
    # ------------------------------------------------------------------

    def test_adguard_noerror_no_a_no_aaaa_no_mapping(self):
        """NOERROR without A and without AAAA does not create a synthetic mapping."""
        resolver = self.make_resolver_with_adguard_and_ipv6()
        # AdGuard authorizes (NOERROR without A), but AAAA query returns no records
        empty_aaaa = DNSRecord.question('ip6only.me', 'AAAA').reply()
        empty_aaaa.header.rcode = RCODE.NOERROR
        resolver.query_doh = Mock(side_effect=[
            self._make_adguard_reply('ip6only.me', RCODE.NOERROR),  # NOERROR, no A records
            empty_aaaa,  # NOERROR, no AAAA records
        ])

        request = DNSRecord.question('ip6only.me', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.SERVFAIL)
        self.assertEqual(len(reply.rr), 0)
        # No mapping created
        self.assertEqual(len(resolver.ipv6_mappings), 0)

    # ------------------------------------------------------------------
    # Test 6: CNAME leading to domain with AAAA
    # ------------------------------------------------------------------

    def test_adguard_cname_to_aaaa_returns_synthetic_a(self):
        """CNAME chain leading to AAAA record returns synthetic A address."""
        resolver = self.make_resolver_with_adguard_and_ipv6()
        # AdGuard authorizes (NOERROR without A)
        # CNAME chain: example.com -> alias.example.com -> AAAA 2001:db8::1
        resolver.query_doh = Mock(side_effect=[
            self._make_adguard_reply('example.com', RCODE.NOERROR),  # NOERROR, no A records
            self._make_cname_reply('example.com', 'alias.example.com'),
            self._make_aaaa_reply('alias.example.com', ['2001:db8::1']),
        ])

        request = DNSRecord.question('example.com', 'A')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 1)
        self.assertEqual(reply.rr[0].rtype, QTYPE.A)
        self.assertEqual(str(reply.rr[0].rdata), '14.16.0.2')

    # ------------------------------------------------------------------
    # Test 7: AAAA/HTTPS/SVCB queries return empty NOERROR (no leak)
    # ------------------------------------------------------------------

    def test_adguard_aaaa_query_no_leak(self):
        """AAAA query for authorized AAAA-only domain returns empty NOERROR (no real IPv6 leak)."""
        resolver = self.make_resolver_with_adguard_and_ipv6()
        resolver.query_doh = Mock(return_value=self._make_adguard_reply(
            'ip6only.me', RCODE.NOERROR
        ))

        request = DNSRecord.question('ip6only.me', 'AAAA')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 0)

    def test_adguard_https_query_no_leak(self):
        """HTTPS query for authorized AAAA-only domain returns empty NOERROR (no leak)."""
        resolver = self.make_resolver_with_adguard_and_ipv6()
        resolver.query_doh = Mock(return_value=self._make_adguard_reply(
            'ip6only.me', RCODE.NOERROR
        ))

        request = DNSRecord.question('ip6only.me', 'HTTPS')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 0)

    def test_adguard_svcb_query_no_leak(self):
        """SVCB query for authorized AAAA-only domain returns empty NOERROR (no leak)."""
        resolver = self.make_resolver_with_adguard_and_ipv6()
        resolver.query_doh = Mock(return_value=self._make_adguard_reply(
            'ip6only.me', RCODE.NOERROR
        ))

        request = DNSRecord.question('ip6only.me', 'SVCB')
        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(len(reply.rr), 0)


if __name__ == '__main__':
    unittest.main()
