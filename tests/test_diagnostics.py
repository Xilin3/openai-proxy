import http.client
import json
import re
import threading
import unittest
from unittest.mock import patch

from bps_proxy.auth import ChatGPTSession
from bps_proxy.server import ProxyServer
from bps_proxy.upstream import UpstreamError
from bps_proxy.wire import CallMemory, ProtocolError, StreamRewriter
from tests.test_transport_contract import TOOLS, completed, native_call


class DiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ProxyServer(("127.0.0.1", 0), CallMemory())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def send(self, headers=None, source=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            body = {"input": "private-prompt", "stream": True, **(source or {})}
            connection.request("POST", "/v1/responses?private-query", json.dumps(body), headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_local_403_is_distinct_and_headers_are_not_logged(self):
        with self.assertLogs("bps_proxy", "INFO") as logs, patch("bps_proxy.server.load_session") as auth:
            status, _ = self.send({"Origin": "https://private-origin.example", "Authorization": "Bearer private-token"})
        self.assertEqual(status, 403)
        auth.assert_not_called()
        text = chr(10).join(logs.output)
        self.assertIn("source=local_guard", text)
        self.assertIn("route=/v1/responses status=403", text)
        for secret in ("private-origin", "private-query", "private-token", "private-prompt"):
            self.assertNotIn(secret, text)

    def test_upstream_403_is_distinct_without_logging_error_body(self):
        with self.assertLogs("bps_proxy", "INFO") as logs, patch("bps_proxy.server.load_session", return_value=ChatGPTSession("fake", "account", "", 0)), patch("bps_proxy.server.iter_events", side_effect=UpstreamError(403, "private-error-body")):
            status, _ = self.send()
        self.assertEqual(status, 403)
        text = chr(10).join(logs.output)
        self.assertIn("source=upstream status=403 stream_started=False", text)
        self.assertIn("terminal=response.failed", text)
        self.assertNotIn("private-error-body", text)
        self.assertNotIn("private-prompt", text)
        ids = re.findall("request_id=([a-f0-9]{12})", text)
        self.assertTrue(ids)
        self.assertEqual(len(set(ids)), 1)

    def test_success_records_formal_terminal_instead_of_only_http_200(self):
        with self.assertLogs("bps_proxy", "INFO") as logs, patch("bps_proxy.server.load_session", return_value=ChatGPTSession("fake", "account", "", 0)), patch("bps_proxy.server.iter_events", return_value=iter([completed([])])):
            status, body = self.send()
        self.assertEqual(status, 200)
        self.assertIn(b"response.completed", body)
        self.assertIn("terminal=response.completed", chr(10).join(logs.output))

    def test_missing_tools_are_diagnosed_without_executing_or_caching_them(self):
        for event, done in (("response.output_item.added", 0), ("response.output_item.done", 1)):
            with self.subTest(event=event):
                memory = CallMemory()
                rewriter = StreamRewriter(TOOLS, memory)
                native = native_call()
                self.assertEqual(rewriter.handle(event, {"output_index": 0, "item": native}), [])
                with self.assertLogs("bps_proxy", "WARNING") as logs, self.assertRaisesRegex(ProtocolError, "omitted an original tool call"):
                    rewriter.handle(*completed([]))
                self.assertIn("missing=1 missing_done=" + str(done), chr(10).join(logs.output))
                self.assertEqual(rewriter.client_calls, [])
                self.assertIsNone(memory.recall(native["call_id"]))
                self.assertNotIn("Tokyo", chr(10).join(logs.output))

    def test_family_routes_log_requested_and_actual_response_models(self):
        for family in ('sol', 'terra', 'luna'):
            requested, actual = 'gpt-6-' + family, 'gpt-5.6-' + family
            event, payload = completed([])
            payload['response']['model'] = actual
            with self.subTest(model=requested), self.assertLogs('bps_proxy', 'INFO') as logs, patch(
                    'bps_proxy.server.load_session', return_value=ChatGPTSession('fake', 'account', '', 0)), patch(
                    'bps_proxy.server.iter_events', return_value=iter([(event, payload)])) as upstream:
                status, body = self.send(source={'model': requested, 'stream': False})
            self.assertEqual(status, 200)
            self.assertEqual(upstream.call_args.args[1]['model'], actual)
            self.assertEqual(json.loads(body)['model'], actual)
            self.assertIn('requested_model=' + requested + ' model=' + actual, chr(10).join(logs.output))

    def test_403_is_not_retried_with_another_model(self):
        for requested, actual in (("gpt-6-sol", "gpt-5.6-sol"), ("gpt-6-terra", "gpt-5.6-terra"),
                                  ("gpt-6-luna", "gpt-5.6-luna"), ("gpt-6-astra", "gpt-6-astra")):
            with self.subTest(model=requested), patch("bps_proxy.server.load_session", return_value=ChatGPTSession("fake", "account", "", 0)), patch("bps_proxy.server.iter_events", side_effect=UpstreamError(403, "denied")) as upstream:
                status, _ = self.send(source={"model": requested})
                self.assertEqual(status, 403)
                self.assertEqual(upstream.call_count, 1)
                self.assertEqual(upstream.call_args.args[1]["model"], actual)

    def test_unlisted_request_model_is_not_logged_verbatim(self):
        private_model = "private-model" + chr(10) + "injected-secret"
        with self.assertLogs("bps_proxy", "INFO") as logs, patch("bps_proxy.server.load_session", return_value=ChatGPTSession("fake", "account", "", 0)), patch("bps_proxy.server.iter_events", return_value=iter([completed([])])):
            status, _ = self.send(source={"model": private_model})
        self.assertEqual(status, 200)
        text = chr(10).join(logs.output)
        self.assertIn("requested_model=unlisted model=unlisted", text)
        self.assertNotIn("private-model", text)
        self.assertNotIn("injected-secret", text)
