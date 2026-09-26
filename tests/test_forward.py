import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bps_proxy.server import Handler
from bps_proxy.upstream import UpstreamError
from bps_proxy.wire import (
    OFFICE_STOP,
    CallMemory,
    iter_client_tools,
    protocol_instructions,
    translate_transport,
)


HTML = '<html>\n<body>"pelican" & dog</body>\n</html>'
TOOLS = [
    {"type": "custom", "name": "exec", "description": "Run a command.", "parameters": {}},
    {"type": "function", "name": "get_weather", "description": "Weather.", "parameters": {"type": "object"}},
]


def office(name, call_id, arguments):
    return {
        "type": "function_call",
        "id": "fc_" + call_id,
        "call_id": call_id,
        "name": name,
        "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
    }


def transport(call_id, code):
    return office(
        "run_officejs",
        call_id,
        {"summary": "save", "code": code, "destructive": False, "references": []},
    )


def completed(output, response_id="resp_test"):
    return "response.completed", {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "output": output,
        },
    }


def assistant(text, message_id="msg_html"):
    return {
        "type": "message",
        "id": message_id,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


class DecodeTest(unittest.TestCase):
    def test_prompt_uses_two_shapes_and_allows_repeated_wrappers(self):
        text = protocol_instructions(iter_client_tools(TOOLS))
        self.assertIn('{"tool":"TOOL_NAME","args":{...}}', text)
        self.assertIn('{"tool":"exec","input":"raw text"}', text)
        self.assertNotIn('"args":"raw text"', text)
        self.assertNotIn("exactly once", text)
        self.assertIn("own wrapper", text)
        self.assertNotIn("exactly once", OFFICE_STOP)
        self.assertIn('{"tool":"TOOL_NAME","input":"raw text"}', OFFICE_STOP)
        self.assertIn("escape backslashes and quotes", text)
        self.assertIn("escape backslashes and quotes", OFFICE_STOP)
        patch_text = protocol_instructions(iter_client_tools([
            {"type": "custom", "name": "apply_patch", "description": "Edit a file.", "parameters": {}}
        ]))
        self.assertIn("Do not use arguments.patch.", patch_text)

    def test_invalid_backslashes_and_nested_wrappers_still_forward(self):
        broken = '{"tool":"exec","input":"rg \\(foo"}'
        call, rejection = translate_transport(
            transport("call_escape", broken),
            {"exec"},
            {"exec"},
        )
        self.assertIsNone(rejection)
        self.assertEqual(call["input"], "rg \\(foo")
        valid = json.dumps({"tool": "exec", "input": "rg \\(foo"})
        kept, rejection = translate_transport(transport("call_kept", valid), {"exec"}, {"exec"})
        self.assertIsNone(rejection)
        self.assertEqual(kept["input"], "rg \\(foo")
        inner = json.dumps({"tool": "exec", "input": "pwd"})
        nested = json.dumps({"name": "run_officejs", "arguments": {"code": inner}})
        unwrapped, rejection = translate_transport(
            transport("call_nested", nested),
            {"exec"},
            {"exec"},
        )
        self.assertIsNone(rejection)
        self.assertEqual(unwrapped["input"], "pwd")

    def test_file_write_survives_broken_json_inside_exec(self):
        html = '<div class="sky">\ncat</div>'
        broken = (
            '{"tool":"exec","input":"text(await tools.exec_command({cmd:"'
            "cat > outputs/cat-rides-pelican.html <<'EOF'\\n"
            + html
            + "\\nEOF\"}));\"}"
        )
        call, rejection = translate_transport(transport("call_save", broken), {"exec"}, {"exec"})
        self.assertIsNone(call)
        self.assertEqual(rejection.reason, 'inner_json')
        # Ambiguous quotes are never guessed in executable scripts; lossless JSON works.
        valid = json.dumps({'tool': 'exec', 'input': html})
        parsed, rejection = translate_transport(transport('call_valid', valid), {'exec'}, {'exec'})
        self.assertIsNone(rejection)
        self.assertEqual(parsed['input'], html)
        office, rejection = translate_transport(
            transport("call_office", "Excel.run(async (context) => context)"),
            {"exec"},
            {"exec"},
        )
        self.assertIsNone(office)
        self.assertEqual(rejection.reason, "inner_json")
        alias, rejection = translate_transport(
            transport("call_alias", json.dumps({"tool": "functions.exec", "input": "pwd"})),
            {"exec"},
            {"exec"},
        )
        self.assertIsNone(rejection)
        self.assertEqual(alias["name"], "exec")
        self.assertEqual(alias["input"], "pwd")

    def test_namespace_tool_and_schema_are_enforced(self):
        namespaced = {
            "type": "namespace",
            "name": "functions",
            "tools": [{"type": "custom", "name": "exec", "description": "Run.", "parameters": {}}],
        }
        call, rejection = translate_transport(
            transport("call_ns", json.dumps({"tool": "functions.exec", "input": "pwd"})),
            {"exec"},
            {"exec"},
        )
        self.assertIsNone(rejection)
        self.assertEqual(call["name"], "exec")
        self.assertEqual(call["input"], "pwd")
        from bps_proxy.wire import iter_client_tools

        self.assertEqual(iter_client_tools([namespaced])[0]["name"], "exec")
        schema = {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        }
        rejected, failure = translate_transport(
            transport("call_schema", json.dumps({"tool": "get_weather", "args": {"city": 1}})),
            {"get_weather"},
            set(),
            {"get_weather": schema},
        )
        self.assertIsNone(rejected)
        self.assertEqual(failure.reason, "bad_arguments")

    def test_client_update_plan_is_forwarded(self):
        from bps_proxy.wire import StreamRewriter

        native = {
            "type": "function_call",
            "id": "fc_plan",
            "call_id": "call_plan",
            "name": "update_plan",
            "arguments": json.dumps(
                {"summary": "Draw", "plan": [{"description": "Save the html", "status": "in_progress"}]}
            ),
        }
        tools = [{"type": "function", "name": "update_plan", "description": "Plan.", "parameters": {}}]
        rewriter = StreamRewriter(tools, CallMemory())
        events = rewriter.handle(
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": 0, "item": native},
        )
        self.assertEqual(events, [])
        events = rewriter.handle(*completed([native]))[:-1]
        call = events[-1][1]["item"]
        self.assertEqual(call["name"], "update_plan")
        self.assertEqual(
            json.loads(call["arguments"]),
            {"plan": [{"step": "Save the html", "status": "in_progress"}], "explanation": "Draw"},
        )

    def test_string_args_still_reach_custom_tools(self):
        native = transport("call_exec", json.dumps({"tool": "exec", "args": "pwd"}))
        call, rejection = translate_transport(native, {"exec"}, {"exec"})
        self.assertIsNone(rejection)
        self.assertEqual(call["type"], "custom_tool_call")
        self.assertEqual(call["input"], "pwd")

    def test_rejection_reasons_stay_distinct(self):
        cases = {
            "outer_json": office("run_officejs", "call_outer", "{"),
            "missing_code": office("run_officejs", "call_missing", {"summary": "save"}),
            "inner_json": transport("call_html", HTML),
            "unknown_tool": transport("call_unknown", json.dumps({"tool": "missing", "args": {}})),
            "bad_arguments": transport("call_bad", json.dumps({"tool": "get_weather", "args": ["Tokyo"]})),
            "missing_call_id": {**transport("call_id", json.dumps({"tool": "exec", "input": "pwd"})), "call_id": ""},
        }
        for reason, item in cases.items():
            with self.subTest(reason=reason):
                call, rejection = translate_transport(item, {"exec", "get_weather"}, {"exec"})
                self.assertIsNone(call)
                self.assertEqual(rejection.reason, reason)
                self.assertNotIn("pelican", rejection.summary)
                self.assertNotIn("<html>", rejection.summary)

    def test_diagnostic_mode_saves_the_sample_outside_the_log(self):
        native = transport("call_html", HTML)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {"BPS_PROXY_DIAGNOSTIC": "1", "BPS_PROXY_DIAGNOSTIC_DIR": directory},
        ), self.assertLogs("bps_proxy", "INFO") as logs:
            from bps_proxy.wire import StreamRewriter

            rewriter = StreamRewriter(iter_client_tools(TOOLS), CallMemory(), turn_id="turn-1", hop=0)
            rewriter.handle("response.output_item.done", {"output_index": 0, "item": native})
            rewriter.handle(*completed([native]))
            logged = "\n".join(logs.output)
            self.assertIn("reason=inner_json", logged)
            self.assertIn("turn_id=turn-1", logged)
            self.assertIn("call_id=call_html", logged)
            self.assertNotIn("pelican", logged)
            saved = list(Path(directory).glob("*.json"))
            self.assertEqual(len(saved), 1)
            self.assertIn("pelican", saved[0].read_text())


class RelayForwardTest(unittest.TestCase):
    def setUp(self):
        self.handler = Handler.__new__(Handler)
        self.handler.server = SimpleNamespace(memory=CallMemory())
        self.source = {"input": "save the html", "tools": TOOLS}

    def relay(self, batches):
        with patch("bps_proxy.server.iter_events", side_effect=[iter(batch) for batch in batches]) as upstream:
            events = list(self.handler._iter_relay(self.source, None))
        return events, upstream

    def test_bad_envelope_is_corrected_once_then_the_client_can_write_the_file(self):
        good = transport("call_save", json.dumps({"tool": "exec", "input": HTML}))
        events, upstream = self.relay([
            [completed([transport("call_bad", HTML)], "resp_bad")],
            [completed([good], "resp_good")],
        ])
        self.assertEqual(upstream.call_count, 2)
        correction = upstream.call_args_list[1].args[1]["input"]
        stub = next(item["output"] for item in correction if item.get("type") == "function_call_output")
        self.assertIn("not forwarded", stub)
        self.assertIn("Nothing ran on the client machine", stub)
        self.assertIn("Reason: inner_json", stub)
        self.assertIn('{"tool":"TOOL_NAME","input":"raw text"}', stub)
        final = events[-1][1]["response"]
        self.assertEqual(final["status"], "completed")
        call = final["output"][0]
        self.assertEqual(call["type"], "custom_tool_call")
        self.assertEqual(call["input"], HTML)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pelican-riding-dog.html"
            path.write_text(call["input"])
            self.assertEqual(path.read_text(), HTML)

    def test_follow_up_without_tools_still_forwards_exec(self):
        exec_tool = {"type": "custom", "name": "exec", "description": "Run a command.", "parameters": {}}
        first = {"input": "save the html", "tools": [exec_tool]}
        pwd = transport("call_pwd", json.dumps({"tool": "exec", "input": "pwd"}))
        save = transport("call_save", json.dumps({"tool": "exec", "input": "cat > outputs/cat-rides-pelican.html"}))
        with patch("bps_proxy.server.iter_events", side_effect=[iter([completed([pwd])])]) as upstream:
            first_events = list(self.handler._iter_relay(first, None))
        self.assertEqual(upstream.call_count, 1)
        call = first_events[-1][1]["response"]["output"][0]
        self.assertEqual(call["name"], "exec")
        follow = {
            "input": [
                {"role": "user", "content": "save the html"},
                call,
                {"type": "custom_tool_call_output", "call_id": call["call_id"], "output": "ok"},
            ]
        }
        with patch("bps_proxy.server.iter_events", side_effect=[iter([completed([save])])]) as upstream:
            events = list(self.handler._iter_relay(follow, None))
        self.assertEqual(upstream.call_count, 1)
        saved = events[-1][1]["response"]["output"][0]
        self.assertEqual(saved["type"], "custom_tool_call")
        self.assertEqual(saved["name"], "exec")
        self.assertIn("cat-rides-pelican.html", saved["input"])

    def test_already_forwarded_calls_are_not_retried(self):
        weather = transport("call_weather", json.dumps({"tool": "get_weather", "args": {"city": "Tokyo"}}))
        events, upstream = self.relay([[completed([weather, office("read_ranges", "call_sheet", {})])]])
        self.assertEqual(upstream.call_count, 1)
        output = events[-1][1]["response"]["output"]
        self.assertEqual([item["name"] for item in output], ["get_weather"])
        self.assertEqual(events[-1][1]["response"]["status"], "completed")

    def test_duplicate_client_calls_are_both_forwarded_once(self):
        first = transport("call_a", json.dumps({"tool": "exec", "input": "pwd"}))
        second = transport("call_b", json.dumps({"tool": "exec", "input": "ls"}))
        events, upstream = self.relay([[completed([first, second])]])
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual([item["input"] for item in events[-1][1]["response"]["output"]], ["pwd", "ls"])

    def test_exhausted_correction_reports_failure(self):
        bad = transport('call_bad', HTML)
        events, upstream = self.relay([[completed([bad])] for _ in range(4)])
        self.assertEqual(upstream.call_count, 4)
        self.assertEqual(events[-1][0], 'response.failed')
        self.assertIn('客户端未执行', events[-1][1]['response']['error']['message'])
        self.assertEqual(events[-1][1]['response']['output'], [])

    def test_upstream_incomplete_is_left_as_incomplete(self):
        events, upstream = self.relay([[
            ("response.incomplete", {
                "type": "response.incomplete",
                "response": {"id": "resp_1", "object": "response", "status": "incomplete", "output": []},
            })
        ]])
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(events[-1][0], "response.incomplete")
        self.assertEqual(events[-1][1]["response"]["status"], "incomplete")

    def test_finished_items_do_not_complete_a_cut_stream(self):
        message = assistant('text emitted before disconnect')
        events, _ = self.relay([[
            ('response.output_item.added', {'output_index': 0, 'item': message}),
            ('response.output_item.done', {'output_index': 0, 'item': message}),
        ]])
        self.assertEqual(events[-1][0], 'response.failed')
        self.assertFalse(any(name == 'response.completed' for name, _ in events))

    def test_commentary_cut_is_not_treated_as_finished(self):
        message = assistant("正在保存")
        message["phase"] = "commentary"
        events, _upstream = self.relay([[
            ("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": message}),
            ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": message}),
        ]])
        self.assertEqual(events[-1][0], "response.failed")

    def test_truncated_stream_fails_instead_of_completing(self):
        events, upstream = self.relay([[("response.created", {"type": "response.created", "response": {"id": "resp_1"}})]])
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(events[-1][0], "response.failed")
        self.assertEqual(events[-1][1]["response"]["status"], "failed")
        self.assertNotIn("response.completed", [event for event, _ in events])

    def test_upstream_error_does_not_retry_a_partial_office_call(self):
        def broken(*_args):
            yield "response.output_item.done", {"output_index": 0, "item": transport("call_bad", HTML)}
            raise UpstreamError(502, "connection reset")

        with patch("bps_proxy.server.iter_events", side_effect=broken) as upstream:
            events = list(self.handler._iter_relay(self.source, None))
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(events[-1][0], "response.failed")
        self.assertEqual(events[-1][1]["response"]["error"]["message"], "connection reset")
        self.assertEqual(events[-1][1]["response"]["status"], "failed")


class IdleStream(unittest.TestCase):
    def test_silence_repeats_in_progress(self):
        from bps_proxy.upstream import iter_events

        created = {"type": "response.created", "response": {"id": "resp_1"}}
        chunks = [
            b"event: response.created\n",
            b"data: " + json.dumps(created).encode() + b"\n",
            b"\n",
        ]

        class Response:
            headers = {'Content-Type': 'text/event-stream'}
            def __init__(self):
                self.pending = list(chunks)
                self.paused = False

            def readline(self, size=-1):
                if self.pending:
                    return self.pending.pop(0)
                if not self.paused:
                    self.paused = True
                    time.sleep(0.2)
                return b""

            def close(self):
                return None

        session = SimpleNamespace(access_token="token", account_id="account", account_user_id="")
        with patch("bps_proxy.upstream.KEEPALIVE_SECONDS", 0.05), patch(
            "bps_proxy.upstream.request.urlopen", return_value=Response()
        ):
            events = list(iter_events(session, {"input": "hi"}))
        names = [event for event, _ in events]
        self.assertEqual(names[0], "response.created")
        self.assertIn("response.in_progress", names)
        keepalive = next(payload for event, payload in events if event == "response.in_progress")
        self.assertEqual(keepalive["response"]["id"], "resp_1")


class PictureUploadTest(unittest.TestCase):
    def test_refused_user_image_is_uploaded_once(self):
        from bps_proxy.images import Pictures

        encoded = base64.b64encode(__import__('tests.fixtures', fromlist=['png']).png()).decode()
        body = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": f"data:image/png;base64,{encoded}"}],
                }
            ]
        }
        session = SimpleNamespace(account_id="account")
        pictures = Pictures()
        inline = pictures.apply(body, session)
        self.assertIn("data:image/png", json.dumps(inline))
        self.assertEqual(pictures.retry_plan(422), "upload")
        pictures.refuse_inline('account', {'message'})
        with patch("bps_proxy.images.upload", return_value="file_abc") as upload:
            uploaded = pictures.apply(body, session)
            again = pictures.apply(body, session)
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(uploaded["input"][0]["content"][0]["file_id"], "file_abc")
        self.assertNotIn("image_url", uploaded["input"][0]["content"][0])
        self.assertEqual(again["input"][0]["content"][0]["file_id"], "file_abc")
        self.assertEqual(pictures.last.reused, {"file_abc"})
        self.assertEqual(pictures.retry_plan(422), "reupload")


if __name__ == "__main__":
    unittest.main()
