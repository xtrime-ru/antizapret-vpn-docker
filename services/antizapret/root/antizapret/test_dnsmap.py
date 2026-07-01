# Run from the repository root:
# docker run --rm -v "$PWD/services/antizapret/root/antizapret:/tests:ro" -w /tests xtrime/antizapret-vpn:6.7.0 python3 -m unittest -v test_dnsmap.py

import queue
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from dnslib import A, DNSRecord, QTYPE, RCODE, RR

import dnsmap


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
            'AS13335 # Cloudflare\n13238\nTelegram Messenger, Inc.\n\n', encoding='utf-8'
        )

        asns, organizations = dnsmap.ProxyResolver.load_asn_list(self.asn_file)

        self.assertEqual(asns, {13335, 13238})
        self.assertEqual(organizations, {'telegrammessengerinc'})

    def test_organization_name_uses_normalized_partial_match(self):
        resolver = self.make_resolver()
        resolver.blocked_organizations = {'telegram'}
        self.asn_database.get.return_value = {
            'autonomous_system_number': 62041,
            'autonomous_system_organization': 'Telegram Messenger, Inc.',
        }

        self.assertTrue(resolver.is_blocked_asn('192.0.2.1'))

    def test_failed_reload_preserves_last_known_good(self):
        resolver = self.make_resolver()
        resolver.blocked_asns = {13335}
        resolver.asn_file = '/missing/asn.txt'

        with self.assertRaises(FileNotFoundError):
            resolver.reload_asn_list()

        self.assertEqual(resolver.blocked_asns, {13335})

    def test_successful_reload_atomically_replaces_sets(self):
        resolver = self.make_resolver()
        resolver.blocked_asns = {13335}
        resolver.blocked_organizations = {'cloudflare'}
        self.asn_file.write_text('AS13238\nYandex\n', encoding='utf-8')

        resolver.reload_asn_list()

        self.assertEqual(resolver.blocked_asns, {13238})
        self.assertEqual(resolver.blocked_organizations, {'yandex'})

    def test_signal_handler_reloads_immediately(self):
        resolver = self.make_resolver()
        self.asn_file.write_text('AS13238\nYandex\n', encoding='utf-8')
        with patch.object(dnsmap.os, 'write') as output:
            resolver.request_asn_reload(None, None)
        self.assertEqual(resolver.blocked_asns, {13238})
        self.assertEqual(resolver.blocked_organizations, {'yandex'})
        output.assert_called_once()

    def test_signal_handler_keeps_old_list_and_logs_reload_failure(self):
        resolver = self.make_resolver()
        resolver.blocked_asns = {13335}
        resolver.asn_file = '/missing/asn.txt'
        with patch.object(dnsmap.os, 'write') as output:
            resolver.request_asn_reload(None, None)
        self.assertEqual(resolver.blocked_asns, {13335})
        self.assertEqual(output.call_args.args[0], 2)


class MappingTests(ResolverTestCase):
    def test_iptables_failure_does_not_commit_and_recycles_address(self):
        self.iptables_runner.return_value = Mock(returncode=1, stderr='failed')
        resolver = self.make_resolver()

        self.assertFalse(resolver.add_mapping('192.0.2.1'))

        self.assertEqual(resolver.ipmap, {})
        self.assertEqual(resolver.unassigned_addresses[0], '14.16.0.2')

    def test_iptables_success_commits_mapping(self):
        resolver = self.make_resolver()

        self.assertEqual(resolver.add_mapping('192.0.2.1'), '14.16.0.2')

        self.assertEqual(resolver.get_mapping('192.0.2.1'), '14.16.0.2')
        command = self.iptables_runner.call_args.args[0]
        self.assertEqual(command[-1], '192.0.2.1')


class ResolverContractTests(ResolverTestCase):
    def resolver_with_replies(self, replies, asn_match=False):
        resolver = self.make_resolver()
        resolver.query_doh = Mock(side_effect=replies)
        resolver.is_blocked_asn = Mock(return_value=asn_match)
        resolver.add_mapping = Mock(side_effect=['14.16.0.2', '14.16.0.3'])
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
        resolver.add_mapping.assert_not_called()

    def test_one_asn_match_maps_all_resolved_addresses(self):
        request = DNSRecord.question('example.com', 'A')
        resolver = self.resolver_with_replies([
            dns_reply(request, rcode=RCODE.SERVFAIL),
            dns_reply(request, ['192.0.2.1', '192.0.2.2']),
        ])
        resolver.is_blocked_asn.side_effect = [False, True]

        reply = resolver.resolve(request, Mock())

        self.assertEqual([str(record.rdata) for record in reply.rr], ['14.16.0.2', '14.16.0.3'])
        self.assertEqual(resolver.add_mapping.call_count, 2)

    def test_nxdomain_is_preserved(self):
        request = DNSRecord.question('missing.example', 'A')
        resolver = self.resolver_with_replies([dns_reply(request, rcode=RCODE.NXDOMAIN)])

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NXDOMAIN)
        resolver.add_mapping.assert_not_called()

    def test_empty_noerror_answer_is_preserved(self):
        request = DNSRecord.question('empty.example', 'A')
        resolver = self.resolver_with_replies([dns_reply(request)])

        reply = resolver.resolve(request, Mock())

        self.assertEqual(reply.header.rcode, RCODE.NOERROR)
        self.assertEqual(reply.rr, [])
        resolver.add_mapping.assert_not_called()

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


if __name__ == '__main__':
    unittest.main()
