import io
import json
import sys
import unittest
from unittest.mock import patch

from bps_proxy.auth import ChatGPTSession
from bps_proxy.server import ProxyServer
from bps_proxy.upstream import MAX_EVENT_BYTES, UpstreamError, iter_events
from bps_proxy.wire import CallMemory


NL = bytes([10])
CRLF = bytes([13, 10])


class Response(io.BytesIO):
    headers = {'Content-Type': 'text/event-stream'}


class SSESizeTests(unittest.TestCase):
    session = ChatGPTSession('fake-token', 'test', '', 0)

    def read(self, raw, **kwargs):
        response = Response(raw)
        try:
            with patch('bps_proxy.upstream.request.urlopen', return_value=response):
                return list(iter_events(self.session, {}, **kwargs))
        finally:
            self.assertTrue(response.closed)

    def test_default_accepts_five_mib_event_and_logs_only_size(self):
        self.assertEqual(MAX_EVENT_BYTES, 16 * 1024 * 1024)
        payload = {'type': 'response.output_item.done',
                   'item': {'type': 'reasoning', 'encrypted_content': 'private-payload-' + 'x' * (5 * 1024 * 1024)}}
        encoded = json.dumps(payload).encode()
        with self.assertLogs('bps_proxy', 'INFO') as logs:
            events = self.read(b'data: ' + encoded + NL * 2, request_id='012345abcdef')
        self.assertEqual(events, [('response.output_item.done', payload)])
        text = chr(10).join(logs.output)
        self.assertIn('request_id=012345abcdef event_type=response.output_item.done', text)
        self.assertIn('event_bytes=' + str(len(encoded)), text)
        self.assertIn('limit_bytes=16777216', text)
        self.assertNotIn('private-payload', text)
        self.assertNotIn('encrypted_content', text)

    def test_line_limit_accepts_exact_size_and_rejects_one_byte_over(self):
        line = b'data: ' + json.dumps({'type': 'response.completed'}).encode() + CRLF
        self.assertEqual(self.read(line + CRLF, max_event_bytes=len(line))[0][0], 'response.completed')
        with self.assertLogs('bps_proxy', 'WARNING') as logs, self.assertRaisesRegex(UpstreamError, 'line was too large'):
            self.read(line + CRLF, max_event_bytes=len(line) - 1)
        self.assertIn('limit_kind=line observed_bytes=' + str(len(line)), chr(10).join(logs.output))

    def test_multiline_event_limit_counts_utf8_bytes_and_separators(self):
        encoded = json.dumps({'a': '中' * 20, 'b': '文' * 20}, ensure_ascii=False, indent=0).encode('utf-8')
        pieces = encoded.split(NL)
        raw = b''.join(b'data: ' + piece + NL for piece in pieces) + NL
        self.assertEqual(self.read(raw, max_event_bytes=len(encoded)), [('message', json.loads(encoded))])
        with self.assertLogs('bps_proxy', 'WARNING') as logs, self.assertRaisesRegex(UpstreamError, 'event was too large'):
            self.read(raw, max_event_bytes=len(encoded) - 1)
        self.assertIn('limit_kind=event observed_bytes=' + str(len(encoded)), chr(10).join(logs.output))

    def test_empty_data_lines_cannot_bypass_event_limit(self):
        with self.assertLogs('bps_proxy', 'WARNING'), self.assertRaisesRegex(UpstreamError, 'event was too large'):
            self.read((b'data:' + NL) * 66, max_event_bytes=64)

    def test_default_rejects_line_larger_than_sixteen_mib(self):
        with self.assertLogs('bps_proxy', 'WARNING') as logs, self.assertRaisesRegex(UpstreamError, 'line was too large'):
            self.read(b'data: ' + b'x' * MAX_EVENT_BYTES + NL * 2)
        self.assertIn('observed_bytes=16777217 limit_bytes=16777216', chr(10).join(logs.output))

    def test_oversized_line_reads_only_bounded_prefix_and_redacts_header(self):
        class TrackedResponse(Response):
            def __init__(self, data):
                super().__init__(data)
                self.read_sizes = []

            def readline(self, size=-1):
                line = super().readline(size)
                self.read_sizes.append((size, len(line)))
                return line

        header = b'event: private-event-secret' + NL
        response = TrackedResponse(header + b'data: private-payload-' + b'x' * 4096 + NL * 2)
        with patch('bps_proxy.upstream.request.urlopen', return_value=response), self.assertLogs('bps_proxy', 'WARNING') as logs:
            with self.assertRaisesRegex(UpstreamError, 'line was too large'):
                list(iter_events(self.session, {}, max_event_bytes=64, request_id='012345abcdef'))
        self.assertTrue(response.closed)
        self.assertEqual(response.read_sizes, [(65, len(header)), (65, 65)])
        text = chr(10).join(logs.output)
        self.assertIn('request_id=012345abcdef event_type=other limit_kind=line observed_bytes=65 limit_bytes=64', text)
        for private in ('private-event', 'private-payload', 'connection failed'):
            self.assertNotIn(private, text)

    def test_large_event_log_redacts_unknown_payload_type(self):
        data = json.dumps({'type': 'private-event-secret', 'text': 'private-payload'}).encode()
        with patch('bps_proxy.upstream.LARGE_EVENT_BYTES', 1), self.assertLogs('bps_proxy', 'INFO') as logs:
            self.read(b'data: ' + data + NL * 2)
        self.assertIn('event_type=other', chr(10).join(logs.output))
        self.assertNotIn('private-', chr(10).join(logs.output))

    def test_invalid_limit_fails_before_opening_connection(self):
        for limit in (0, -1, True, 1.5, '64', sys.maxsize):
            with self.subTest(limit=limit), patch('bps_proxy.upstream.request.urlopen') as open_upstream:
                with self.assertRaises(ValueError):
                    list(iter_events(self.session, {}, max_event_bytes=limit))
                open_upstream.assert_not_called()

    def test_server_limits_are_independent(self):
        first = ProxyServer(('127.0.0.1', 0), CallMemory(), max_sse_event_bytes=64)
        self.addCleanup(first.server_close)
        second = ProxyServer(('127.0.0.1', 0), CallMemory(), max_sse_event_bytes=128)
        self.addCleanup(second.server_close)
        default = ProxyServer(('127.0.0.1', 0), CallMemory())
        self.addCleanup(default.server_close)
        self.assertEqual((first.max_sse_event_bytes, second.max_sse_event_bytes), (64, 128))
        self.assertEqual(default.max_sse_event_bytes, 16 * 1024 * 1024)

    def test_invalid_server_limit_fails_before_binding(self):
        for limit in (0, -1, True, 1.5, '64', sys.maxsize):
            with self.subTest(limit=limit), patch('bps_proxy.server.ThreadingHTTPServer.__init__') as bind:
                with self.assertRaises(ValueError):
                    ProxyServer(('127.0.0.1', 0), CallMemory(), max_sse_event_bytes=limit)
                bind.assert_not_called()
