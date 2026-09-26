import http.client
import io
import json
import socket
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bps_proxy.server import Handler, ProxyServer
from bps_proxy.upstream import UpstreamError
from bps_proxy.auth import ChatGPTSession
from bps_proxy.wire import CallMemory


def terminal(event="response.completed", output=None, response_id="resp_test"):
    return event, {"type": event, "sequence_number": 100, "response": {
        "id": response_id, "object": "response", "status": event.split(".")[-1],
        "output": output or [], "instructions": "upstream private instructions",
    }}


def office_call(call_id="call_office"):
    return {"type": "function_call", "id": "fc_" + call_id, "call_id": call_id,
            "name": "read_ranges", "arguments": "{}"}


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.handler = Handler.__new__(Handler)
        self.handler.server = SimpleNamespace(memory=CallMemory())

    def relay(self, batches, source=None):
        with patch("bps_proxy.server.iter_events", side_effect=[iter(batch) for batch in batches]):
            return list(self.handler._iter_relay(source or {"input": "hi"}, None))

    def test_truncated_stream_fails(self):
        events = self.relay([[('response.created', {"type": "response.created", "response": {"id": "resp_1"}})]])
        self.assertEqual(events[-1][0], "response.failed")
        self.assertEqual(events[-1][1]["response"]["id"], "resp_1")

    def test_terminal_without_output_still_completes(self):
        events = self.relay([[('response.completed', {"type": "response.completed", "response": {"id": "resp_1"}})]])
        self.assertEqual(events[-1][0], "response.completed")
        self.assertEqual(events[-1][1]["response"]["output"], [])

    def test_office_retry_limit_is_a_failed_response(self):
        events = self.relay([[terminal(output=[office_call()])] for _ in range(4)])
        self.assertEqual(events[-1][0], 'response.failed')
        self.assertFalse(any(event == 'response.completed' for event, _ in events))

    def test_incomplete_is_a_terminal_response(self):
        events = self.relay([[terminal("response.incomplete")]])
        self.assertEqual(events[-1][0], "response.incomplete")
        self.assertEqual(events[-1][1]["response"]["instructions"], "")

    def test_failed_response_does_not_retry_office_calls(self):
        events = self.relay([[terminal("response.failed", [office_call()])]])
        self.assertEqual(events[-1][0], "response.failed")
        self.assertEqual(events[-1][1]["response"]["output"], [])

    def test_upstream_error_event_preserves_message(self):
        events = self.relay([[('error', {'type': 'error', 'message': 'rate limit'})]])
        self.assertEqual(events[-1][0], 'response.failed')
        self.assertEqual(events[-1][1]['response']['error']['message'], 'rate limit')

    def test_closing_relay_releases_upstream(self):
        closed = []

        def upstream(*_):
            try:
                yield "response.created", {"type": "response.created", "response": {"id": "resp_1"}}
                yield terminal()
            finally:
                closed.append(True)

        with patch("bps_proxy.server.iter_events", side_effect=upstream):
            relay = self.handler._iter_relay({"input": "hi"}, None)
            next(relay)
            relay.close()
        self.assertEqual(closed, [True])

    def test_hops_share_id_indices_sequence_and_complete_output(self):
        first = {"type": "message", "id": "msg_first", "role": "assistant", "content": []}
        second = {"type": "message", "id": "msg_second", "role": "assistant", "content": []}
        batches = []
        for number, message in enumerate((first, second)):
            output = [office_call(), message] if number == 0 else [message]
            batch = [('response.created', {"type": "response.created", "response": {"id": f"resp_{number}"}})]
            for index, item in enumerate(output):
                for suffix in ("added", "done"):
                    name = f"response.output_item.{suffix}"
                    batch.append((name, {"type": name, "output_index": index, "item": item}))
            batch.append(terminal(output=output, response_id=f"resp_{number}"))
            batches.append(batch)
        with patch("bps_proxy.server.iter_events", side_effect=[iter(b) for b in batches]) as upstream:
            events = list(self.handler._iter_relay({"input": "hi"}, None))
        added = [payload for event, payload in events if event == "response.output_item.added"]
        self.assertEqual([p["output_index"] for p in added], [0, 1])
        self.assertEqual([p["sequence_number"] for _, p in events], list(range(len(events))))
        self.assertEqual({p["response"]["id"] for _, p in events if "response" in p}, {"resp_0"})
        self.assertEqual([item["id"] for item in events[-1][1]["response"]["output"]], ["msg_first", "msg_second"])
        next_input = upstream.call_args_list[1].args[1]["input"]
        self.assertTrue(any(item.get("role") == "assistant" for item in next_input))


class HttpTest(unittest.TestCase):
    def setUp(self):
        auth = patch("bps_proxy.server.load_session", return_value=None)
        upstream = patch("bps_proxy.server.iter_events", side_effect=lambda *_: iter([terminal()]))
        auth.start()
        upstream.start()
        self.addCleanup(auth.stop)
        self.addCleanup(upstream.stop)

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

    def send(self, body=b'{"input":"hi"}', headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request("POST", "/v1/responses", body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_invalid_content_lengths_are_rejected(self):
        for length in ("nope", "-1"):
            with self.subTest(length=length):
                status, _ = self.send(headers={"Content-Length": length})
                self.assertEqual(status, 400)

    def test_chunked_body_is_rejected(self):
        status, _ = self.send(headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 400)

    def test_browser_origin_is_rejected_before_auth(self):
        with patch("bps_proxy.server.load_session") as auth:
            status, _ = self.send(headers={"Origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        auth.assert_not_called()

    def test_dns_rebinding_host_is_rejected(self):
        status, _ = self.send(headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)

    def test_non_boolean_stream_is_rejected(self):
        status, _ = self.send(json.dumps({"input": "hi", "stream": "false"}).encode())
        self.assertEqual(status, 400)

    def test_oversized_request_rejected_before_body_is_read(self):
        status, _ = self.send(headers={"Content-Length": str(33 * 1024 * 1024)})
        self.assertEqual(status, 413)

    def test_invalid_inputs_are_rejected_before_auth(self):
        for body in (b"[]", b"{", b'{"input":42}', b'{"input":"hi","temperature":NaN}',
                     b'{"input":"hi","previous_response_id":"resp_previous"}',
                     b'{"input":"hi","background":true}'):
            with self.subTest(body=body), patch("bps_proxy.server.load_session") as auth:
                status, _ = self.send(body)
                self.assertEqual(status, 400)
                auth.assert_not_called()

    def test_duplicate_content_length_is_rejected(self):
        with socket.create_connection(self.server.server_address, timeout=3) as connection:
            connection.sendall(b'POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n{}')
            self.assertIn(b" 400 ", connection.recv(4096).split(b"\r\n")[0])

    def test_expect_continue_rejects_oversized_body_immediately(self):
        with socket.create_connection(self.server.server_address, timeout=3) as connection:
            connection.sendall(b'POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nContent-Length: 999999999\r\nExpect: 100-continue\r\n\r\n')
            self.assertIn(b" 413 ", connection.recv(4096).split(b"\r\n")[0])

    def test_truncated_sse_ends_with_failed(self):
        with patch("bps_proxy.server.iter_events", return_value=iter([])):
            status, body = self.send(b'{"input":"hi","stream":true}')
        self.assertEqual(status, 200)
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith(b"data: {")]
        self.assertEqual(events[-1]["type"], "response.failed")
        self.assertEqual(events[-1]["response"]["status"], "failed")
        self.assertIn(b"[DONE]", body)

    def test_unknown_exception_does_not_expose_details(self):
        with patch("bps_proxy.server.iter_events", side_effect=RuntimeError("private-debug-data")), self.assertLogs("bps_proxy", "ERROR"):
            status, body = self.send()
        self.assertEqual(status, 502)
        self.assertNotIn(b"private-debug-data", body)

    def test_auth_error_is_returned_as_401(self):
        from bps_proxy.auth import AuthError
        with patch("bps_proxy.server.load_session", side_effect=AuthError("please log in")):
            status, body = self.send()
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["type"], "authentication_error")

    def test_upstream_http_error_keeps_status(self):
        with patch("bps_proxy.server.iter_events", side_effect=UpstreamError(429, "rate limit")):
            status, body = self.send()
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"]["message"], "rate limit")

    def test_real_sse_parser_and_http_response_work_together(self):
        from bps_proxy.upstream import iter_events
        message = {"type": "message", "id": "msg_1", "role": "assistant",
                   "content": [{"type": "output_text", "text": "你好"}]}
        raw_events = [("response.created", {"type": "response.created", "response": {"id": "resp_test"}}),
                      terminal(output=[message])]
        for stream in (False, True):
            with self.subTest(stream=stream):
                data = "".join(f"event: {event}\ndata: {json.dumps(payload)}\n\n" for event, payload in raw_events)
                upstream_response = io.BytesIO(data.encode())
                upstream_response.headers = {"Content-Type": "text/event-stream"}
                session = ChatGPTSession("fake-token", "test", "", 0)
                with patch("bps_proxy.server.load_session", return_value=session), patch(
                    "bps_proxy.server.iter_events", iter_events
                ), patch("bps_proxy.upstream.request.urlopen", return_value=upstream_response):
                    status, body = self.send(json.dumps({"input": "hi", "stream": stream}).encode())
                self.assertEqual(status, 200)
                self.assertTrue(upstream_response.closed)
                if stream:
                    self.assertTrue(body.endswith(b"data: [DONE]\n\n"))
                    self.assertIn(b"event: response.completed", body)
                else:
                    self.assertEqual(json.loads(body)["output"][0]["content"][0]["text"], "你好")

    def test_non_loopback_binding_is_rejected(self):
        with self.assertRaises(ValueError):
            ProxyServer(("0.0.0.0", 0), CallMemory())

    def test_incomplete_non_streaming_returns_response(self):
        with patch("bps_proxy.server.load_session", return_value=None), patch(
            "bps_proxy.server.iter_events", return_value=iter([terminal("response.incomplete")])
        ):
            status, body = self.send(b'{"input":"hi","stream":false}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "incomplete")

    def test_invalid_required_selection_returns_400_before_upstream(self):
        for stream in (False, True):
            with self.subTest(stream=stream), patch("bps_proxy.server.iter_events") as upstream:
                status, body = self.send(json.dumps({"input": "run", "tools": [],
                    "tool_choice": "required", "stream": stream}).encode())
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(body)["error"]["type"], "invalid_request_error")
            upstream.assert_not_called()

    def test_required_call_recovery_and_exhaustion_over_http(self):
        from tests.test_forward import assistant, completed, transport
        from tests.test_native_custom import EXEC
        native = transport("http_call", json.dumps({"tool": "exec", "input": "text(2);"}))
        for stream in (False, True):
            for recover in (False, True):
                with self.subTest(stream=stream, recover=recover):
                    batches = [[completed([assistant("Will do.", "msg_first")])],
                               [completed([native] if recover else [assistant("Will do.", "msg_last")])]]
                    with patch("bps_proxy.server.iter_events", side_effect=[iter(b) for b in batches]) as upstream:
                        status, body = self.send(json.dumps({"input": "run", "tools": [EXEC],
                            "tool_choice": "required", "stream": stream}).encode())
                    self.assertEqual(status, 200)
                    self.assertEqual(upstream.call_count, 2)
                    if stream:
                        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith(b"data: {")]
                        terminals = [e for e in events if e["type"] in ("response.completed", "response.failed")]
                        self.assertEqual(len(terminals), 1)
                        result = terminals[0]["response"]
                        self.assertTrue(body.endswith(b"data: [DONE]\n\n"))
                    else:
                        result = json.loads(body)
                    self.assertEqual(result["status"], "completed" if recover else "failed")
                    calls = [i for i in result["output"] if i["type"] == "custom_tool_call"]
                    self.assertEqual(len(calls), int(recover))
                    if not recover:
                        self.assertEqual(result["error"]["code"], "tool_required")


if __name__ == "__main__":
    unittest.main()
