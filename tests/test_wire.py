import json
import tempfile
import unittest
from pathlib import Path

from bps_proxy.wire import (
    CallMemory,
    StreamRewriter,
    client_call_from_native,
    continue_message,
    model_catalog,
    normalize_effort,
    office_stub,
    prepare_body,
    reject_reason,
    translate_input,
)


def _native(call_id: str = "call_QsjZ") -> dict:
    return {
        "type": "function_call",
        "id": "fc_1",
        "call_id": call_id,
        "name": "run_officejs",
        "arguments": json.dumps(
            {
                "summary": "Get current weather for Tokyo",
                "code": json.dumps({"tool": "get_weather", "args": {"city": "Tokyo"}}),
                "destructive": False,
                "references": ["Tokyo weather"],
            }
        ),
    }


class WireTest(unittest.TestCase):
    def test_luna_compatibility_alias_does_not_redirect_other_models(self):
        cases = {"gpt-6-luna": "gpt-5.6-luna", " gpt-6-luna ": "gpt-5.6-luna",
                 "gpt-5.6-luna": "gpt-5.6-luna", "gpt-6-astra": "gpt-6-astra",
                 "gpt-6-sol": "gpt-6-sol", "gpt-6-luna-preview": "gpt-6-luna-preview",
                 "gpt-5.6-luna-excel": "gpt-5.6-luna"}
        for requested, expected in cases.items():
            with self.subTest(model=requested):
                source = {"model": requested, "input": "Generate a title", "reasoning": {"effort": "high"}}
                body = prepare_body(source, CallMemory())
                self.assertEqual(body["model"], expected)
                self.assertEqual(body["model_selection"], "explicit")
                self.assertEqual(body["reasoning_effort"], "high")
                self.assertEqual(source["model"], requested)

    def test_effort_has_no_max_tier(self) -> None:
        self.assertEqual(normalize_effort("max"), "xhigh")
        self.assertEqual(normalize_effort("ultra"), "ultra")
        self.assertEqual(normalize_effort("high"), "high")
        self.assertEqual(normalize_effort("nope"), "medium")
        catalog = model_catalog()
        efforts = [level["effort"] for level in catalog[0]["supported_reasoning_levels"]]
        self.assertEqual(efforts, ["low", "medium", "high", "xhigh", "max", "ultra"])
        self.assertTrue(all(model["shell_type"] == "unified_exec" for model in catalog))
        self.assertTrue(all(model["supported_in_api"] is True for model in catalog))

    def test_ultra_passes_through_and_max_maps_to_xhigh(self) -> None:
        memory = CallMemory()
        body = prepare_body(
            {
                "model": "gpt-6-astra",
                "reasoning": {"effort": "ultra"},
                "input": [
                    {"role": "user", "content": "hi"},
                    {"type": "configuration_update", "reasoning_effort": "max"},
                ],
            },
            memory,
        )
        self.assertEqual(body["reasoning_effort"], "ultra")
        update = body["input"][-1]
        self.assertEqual(update["type"], "configuration_update")
        self.assertEqual(update["reasoning_effort"], "xhigh")

    def test_prepare_strips_tools_and_keeps_turn(self) -> None:
        memory = CallMemory()
        source = {
            "model": "gpt-6-astra",
            "instructions": "You are Codex.",
            "reasoning": {"effort": "max"},
            "tools": [
                {
                    "type": "function",
                    "name": "get_weather",
                    "description": "Look up weather.",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string", "description": "City name"}},
                        "required": ["city"],
                    },
                }
            ],
            "input": [{"role": "user", "content": "weather in Tokyo"}],
        }
        first = prepare_body(source, memory)
        self.assertNotIn("tools", first)
        self.assertEqual(first["model"], "gpt-6-astra")
        self.assertEqual(first["reasoning_effort"], "xhigh")
        self.assertEqual(first["metadata"]["agent_iteration"], "1")
        catalog = first["input"][1]["content"][0]["text"]
        self.assertIn("run_officejs", catalog)
        self.assertIn('"tool":"TOOL_NAME","args":{...}', catalog)
        self.assertIn("get_weather", catalog)
        self.assertNotIn('"type": "object"', catalog)

        source["input"] = [
            {"role": "user", "content": "weather in Tokyo"},
            {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "18C"},
        ]
        second = prepare_body(source, memory)
        self.assertEqual(first["metadata"]["turn_id"], second["metadata"]["turn_id"])
        self.assertEqual(first["metadata"]["task_id"], second["metadata"]["task_id"])
        self.assertEqual(second["metadata"]["agent_iteration"], "2")

    def test_transport_round_trip(self) -> None:
        memory = CallMemory()
        native = _native()
        rewritten = client_call_from_native(native, {"get_weather"})
        self.assertIsNotNone(rewritten)
        assert rewritten is not None
        self.assertEqual(rewritten["name"], "get_weather")
        self.assertEqual(json.loads(rewritten["arguments"]), {"city": "Tokyo"})

        memory.remember(native)
        replayed = translate_input(
            [
                rewritten,
                {"type": "function_call_output", "call_id": "call_QsjZ", "output": "18C"},
            ],
            memory,
        )
        self.assertEqual(replayed[0]["name"], "run_officejs")
        self.assertEqual(replayed[0]["id"], "fc_1")
        arguments = json.loads(replayed[0]["arguments"])
        self.assertEqual(arguments["summary"], "Get current weather for Tokyo")
        self.assertEqual(arguments["references"], ["Tokyo weather"])
        self.assertEqual(json.loads(arguments["code"])["tool"], "get_weather")
        self.assertEqual(replayed[1]["output"], "18C")

    def test_stream_rewriter_emits_client_tool(self) -> None:
        memory = CallMemory()
        tools = [{"name": "get_weather", "description": "", "parameters": {}, "type": "function"}]
        rewriter = StreamRewriter(tools, memory)
        native = _native()
        events = []
        events.extend(
            rewriter.handle(
                "response.output_item.added",
                {"type": "response.output_item.added", "output_index": 0, "item": {**native, "arguments": ""}},
            )
        )
        events.extend(
            rewriter.handle(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": 0, "item": native},
            )
        )
        self.assertEqual(events, [])
        events = rewriter.handle('response.completed', {'response': {'output': [native]}})[:-1]
        self.assertEqual([event for event, _ in events], [
            "response.output_item.added",
            "response.function_call_arguments.done",
            "response.output_item.done",
        ])
        self.assertEqual(events[-1][1]["item"]["name"], "get_weather")
        self.assertIsNotNone(memory.recall("call_QsjZ"))

    def test_memory_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.json"
            memory = CallMemory(path)
            memory.remember(_native())
            restored = CallMemory(path)
            self.assertEqual(restored.recall("call_QsjZ")["id"], "fc_1")

    def test_custom_tool_round_trip(self) -> None:
        memory = CallMemory()
        native = _native("call_exec")
        native["arguments"] = json.dumps(
            {
                "summary": "Run pwd",
                "code": json.dumps({"tool": "exec", "input": "pwd"}),
                "destructive": False,
                "references": [],
            }
        )
        tools = [{"type": "custom", "name": "exec", "description": "Run.", "parameters": {}}]
        rewriter = StreamRewriter(tools, memory)
        events = rewriter.handle(
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": 0, "item": native},
        )
        self.assertEqual(events, [])
        events = rewriter.handle('response.completed', {'response': {'output': [native]}})[:-1]
        call = events[-1][1]["item"]
        self.assertEqual(call["type"], "custom_tool_call")
        self.assertEqual(call["input"], "pwd")
        replayed = translate_input(
            [
                {"type": "custom_tool_call", "id": "ctc_1", "call_id": "call_exec", "name": "exec", "input": "pwd"},
                {
                    "type": "custom_tool_call_output",
                    "id": "ctco_01a0d495-4d89-70a0-bf24-f5bfbdd97e95",
                    "call_id": "call_exec",
                    "output": "ok",
                },
            ],
            memory,
        )
        self.assertEqual(replayed[0]["name"], "run_officejs")
        self.assertTrue(str(replayed[0]["id"]).startswith("fc"))
        self.assertTrue(str(replayed[1]["id"]).startswith("fc_"))
        self.assertFalse(str(replayed[1]["id"]).startswith("ctco_"))

    def test_continue_message_uses_this_requests_tools(self) -> None:
        text = continue_message(
            [
                {"type": "function", "name": "exec_command", "description": "", "parameters": {}},
                {"type": "custom", "name": "apply_patch", "description": "", "parameters": {}},
            ]
        )
        self.assertIn("That call did not run.", text)
        self.assertIn("exec_command", text)
        self.assertIn("apply_patch", text)
        self.assertIn('"args"', text)
        self.assertIn('"input"', text)
        self.assertNotIn("html", text.lower())
        self.assertNotIn("file", text.lower())
        same = office_stub(
            {"name": "run_officejs"},
            [{"type": "function", "name": "exec_command", "description": "", "parameters": {}}],
        )
        self.assertIn("exec_command", same)
        self.assertEqual(office_stub({"name": "update_plan"}), '{"status":"ok"}')

    def test_reject_reason_names_the_failure_without_the_payload(self) -> None:
        html = "<html>" + ("x" * 200)
        not_envelope = {
            "type": "function_call",
            "name": "run_officejs",
            "call_id": "call_1",
            "arguments": json.dumps({"summary": "draw", "code": html}),
        }
        self.assertEqual(reject_reason(not_envelope, {"exec_command"}), "not_envelope")
        self.assertNotIn("html", reject_reason(not_envelope, {"exec_command"}))
        unknown = {
            "type": "function_call",
            "name": "run_officejs",
            "call_id": "call_2",
            "arguments": json.dumps({"code": json.dumps({"tool": "exec", "args": {"cmd": "echo hi"}})}),
        }
        self.assertEqual(reject_reason(unknown, {"exec_command"}), "tool_not_allowed:exec")
        workbook = {"type": "function_call", "name": "read_ranges", "call_id": "call_3", "arguments": "{}"}
        self.assertEqual(reject_reason(workbook, {"exec_command"}), "workbook:read_ranges")

    def test_exec_command_runs_through_the_exec_tool(self) -> None:
        native = {
            "type": "function_call",
            "id": "fc_shell",
            "call_id": "call_shell",
            "name": "run_officejs",
            "arguments": json.dumps(
                {
                    "summary": "Save the page",
                    "code": json.dumps(
                        {
                            "tool": "exec_command",
                            "args": {
                                "cmd": "mkdir -p outputs && cat > outputs/page.html <<'EOF'\nhello\nEOF",
                                "workdir": "/tmp/demo",
                            },
                        }
                    ),
                }
            ),
        }
        tools = [{"type": "custom", "name": "exec", "description": "Run.", "parameters": {}}]
        call = client_call_from_native(native, {"exec"}, {"exec"})
        self.assertIsNotNone(call)
        self.assertEqual(call["type"], "custom_tool_call")
        self.assertEqual(call["name"], "exec")
        self.assertIn("tools.exec_command", call["input"])
        self.assertIn("outputs/page.html", call["input"])
        self.assertIn("/tmp/demo", call["input"])
        direct = client_call_from_native(native, {"exec_command"}, set())
        self.assertEqual(direct["type"], "function_call")
        self.assertEqual(direct["name"], "exec_command")
        implied = client_call_from_native(native, set(), set())
        self.assertIsNone(implied)

    def test_exec_input_keeps_raw_newlines(self) -> None:
        code = '{"tool":"exec","input":"const html = String.raw`<!doctype html>\n<html></html>\n`;"}'
        native = {
            "type": "function_call",
            "call_id": "call_html",
            "name": "run_officejs",
            "arguments": json.dumps({"code": code}),
        }
        call = client_call_from_native(native, {"exec"}, {"exec"})
        self.assertEqual(call["name"], "exec")
        self.assertIn("<!doctype html>\n<html></html>", call["input"])

    def test_exec_code_argument_is_the_script(self) -> None:
        script = "text(await tools.exec_command({cmd:'pwd'}));"
        native = {
            "type": "function_call",
            "call_id": "call_js",
            "name": "run_officejs",
            "arguments": json.dumps({"code": json.dumps({"tool": "exec", "args": {"code": script}})}),
        }
        call = client_call_from_native(native, {"exec"}, {"exec"})
        self.assertEqual(call["input"], script)

    def test_later_request_keeps_the_exec_tool(self) -> None:
        memory = CallMemory()
        first = {
            "prompt_cache_key": "sess-1",
            "tools": [{"type": "custom", "name": "exec", "description": "Run.", "parameters": {}}],
            "input": [{"role": "user", "content": "draw"}],
        }
        self.assertEqual([tool["name"] for tool in memory.bind_tools(first)], ["exec"])
        native = {
            "type": "function_call",
            "call_id": "call_save",
            "name": "run_officejs",
            "arguments": json.dumps({"code": json.dumps({"tool": "exec_command", "args": {"cmd": "pwd"}})}),
        }
        later = {"prompt_cache_key": "sess-1", "tools": [], "input": first["input"]}
        tools = memory.bind_tools(later)
        names = {tool["name"] for tool in tools}
        custom = {tool["name"] for tool in tools if tool.get("type") == "custom"}
        call = client_call_from_native(native, names, custom)
        self.assertEqual(call["name"], "exec")
        self.assertIn("pwd", call["input"])
        other = memory.bind_tools({"prompt_cache_key": "sess-2", "tools": [], "input": first["input"]})
        self.assertEqual(other, [])

    def test_namespace_tools_are_visible(self) -> None:
        from bps_proxy.wire import iter_client_tools

        tools = iter_client_tools(
            [
                {
                    "type": "namespace",
                    "name": "codex",
                    "tools": [{"type": "custom", "name": "exec", "description": "Run.", "parameters": {}}],
                }
            ]
        )
        self.assertEqual([(tool["name"], tool["type"]) for tool in tools], [("exec", "custom")])


if __name__ == "__main__":
    unittest.main()
