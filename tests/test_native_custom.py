"""Desktop Code Mode regression: native custom calls must survive the terminal barrier."""
import copy
import unittest

from bps_proxy.wire import CallMemory, ProtocolError, StreamRewriter, prepare_body, translate_input

EXEC = {"type": "custom", "name": "exec", "description": "Run JavaScript.", "parameters": {}}
DECLARATION = "When calling `functions.exec`, use the supplied tools namespace."


def native(call_id="call_exec"):
    return {"type": "custom_tool_call", "id": "ctc_" + call_id, "call_id": call_id,
            "name": "exec", "status": "completed",
            "input": "const path = String.raw`a" + chr(92) + "b`;" + chr(10) + "text(path);"}


def complete(items):
    return "response.completed", {"response": {"id": "resp_1", "status": "completed", "output": items}}


class NativeCustomTest(unittest.TestCase):
    def test_native_custom_waits_for_terminal_and_preserves_full_item(self):
        memory = CallMemory()
        rewriter = StreamRewriter([EXEC], memory)
        item = native()
        original = copy.deepcopy(item)
        added = {**item, "status": "in_progress", "input": ""}
        self.assertEqual(rewriter.handle("response.output_item.added", {"output_index": 0, "item": added}), [])
        self.assertEqual(rewriter.handle("response.custom_tool_call_input.delta", {"output_index": 0, "delta": item["input"]}), [])
        self.assertEqual(rewriter.handle("response.output_item.done", {"output_index": 0, "item": item}), [])
        self.assertIsNone(memory.recall(item["call_id"]))
        events = rewriter.handle(*complete([item]))
        self.assertEqual([name for name, _ in events], ["response.output_item.added",
                         "response.custom_tool_call_input.done", "response.output_item.done", "response.completed"])
        self.assertEqual(events[-1][1]["response"]["output"], [original])
        self.assertEqual(events[1][1]["input"], original["input"])
        self.assertEqual(memory.recall(item["call_id"]), original)
        self.assertEqual(item, original)
        self.assertEqual(rewriter.handle(*complete([item])), [])

    def test_native_custom_replay_keeps_matching_output_type(self):
        memory = CallMemory()
        item = native()
        memory.remember(item)
        output = {"type": "custom_tool_call_output", "id": "ctco_result",
                  "call_id": item["call_id"], "output": "ok"}
        self.assertEqual(translate_input([output], memory), [item, output])
        self.assertEqual(translate_input([item, output], memory), [item, output])

    def test_non_completed_terminal_never_releases_native_custom(self):
        for state in ("failed", "incomplete"):
            with self.subTest(state=state):
                memory = CallMemory()
                rewriter = StreamRewriter([EXEC], memory)
                item = native()
                rewriter.handle("response.output_item.done", {"output_index": 0, "item": item})
                event, payload = complete([item])
                payload["response"]["status"] = state
                events = rewriter.handle("response." + state, payload)
                self.assertEqual(events[-1][1]["response"]["output"], [])
                self.assertIsNone(memory.recall(item["call_id"]))
                self.assertEqual(rewriter.client_calls, [])

    def test_native_custom_is_not_authorized_by_model_output(self):
        for tools in ([], [{**EXEC, "type": "function"}], [{**EXEC, "name": "another"}]):
            with self.subTest(tools=tools):
                memory = CallMemory()
                rewriter = StreamRewriter(tools, memory)
                with self.assertRaisesRegex(ProtocolError, "unknown_tool"):
                    rewriter.handle(*complete([native()]))
                self.assertIsNone(memory.recall("call_exec"))

    def test_missing_or_duplicate_custom_calls_fail_before_caching(self):
        for output in ([], [native(), native()]):
            with self.subTest(output=output):
                memory = CallMemory()
                rewriter = StreamRewriter([EXEC], memory)
                rewriter.handle("response.output_item.done", {"output_index": 0, "item": native()})
                with self.assertRaises(ProtocolError):
                    rewriter.handle(*complete(output))
                self.assertIsNone(memory.recall("call_exec"))

    def test_malformed_or_unfinished_custom_payload_is_rejected(self):
        for patch in ({"input": {}}, {"call_id": ""}, {"status": "in_progress"}):
            with self.subTest(patch=patch):
                memory = CallMemory()
                rewriter = StreamRewriter([EXEC], memory)
                with self.assertRaises(ProtocolError):
                    rewriter.handle(*complete([{**native(), **patch}]))
                self.assertEqual(rewriter.client_calls, [])

    def test_sequential_request_rejects_multiple_native_calls(self):
        memory = CallMemory()
        rewriter = StreamRewriter([EXEC], memory, parallel=False)
        with self.assertRaisesRegex(ProtocolError, "parallel"):
            rewriter.handle(*complete([native("a"), native("b")]))
        self.assertIsNone(memory.recall("a"))


class CodeModeBindingTest(unittest.TestCase):
    def test_trusted_developer_marker_enables_only_exec_without_tools_array(self):
        for extra in ({"instructions": DECLARATION},
                      {"input": [{"role": "developer", "content": [{"type": "input_text", "text": DECLARATION}]}]}):
            with self.subTest(extra=extra):
                memory = CallMemory()
                tools = memory.bind_tools(extra)
                self.assertEqual([(item["name"], item["type"]) for item in tools], [("exec", "custom")])
                body = prepare_body(extra, memory)
                self.assertNotIn("tools", body)
                self.assertIn("Tool `exec`", str(body["input"]))

    def test_user_text_does_not_declare_tools(self):
        for role in ("user", "assistant", "tool"):
            self.assertEqual(CallMemory().bind_tools({"input": [{"role": role, "content": DECLARATION}]}), [])
        self.assertEqual(CallMemory().bind_tools({"instructions": "Please execute this task"}), [])

    def test_explicit_tools_and_tool_choice_remain_authoritative(self):
        for extra in ({"tools": []}, {"tool_choice": "none"}, {"tool_choice": {"type": "custom", "name": "other"}}):
            self.assertEqual(CallMemory().bind_tools({"instructions": DECLARATION, **extra}), [])
        explicit = {"type": "function", "name": "weather", "parameters": {}}
        tools = CallMemory().bind_tools({"instructions": DECLARATION, "tools": [explicit]})
        self.assertEqual([tool["name"] for tool in tools], ["weather"])

    def test_omitted_tools_preserve_the_previous_explicit_catalog(self):
        memory = CallMemory()
        source = {"prompt_cache_key": "same-client", "instructions": DECLARATION,
                  "tools": [{"type": "function", "name": "weather", "parameters": {}}]}
        first = memory.bind_tools(source)
        del source["tools"]
        self.assertEqual(memory.bind_tools(source), first)
        self.assertEqual(memory.bind_tools({**source, "tool_choice": "none"}), [])

    def test_inferred_executor_does_not_leak_into_another_conversation(self):
        memory = CallMemory()
        self.assertEqual(len(memory.bind_tools({"prompt_cache_key": "desktop", "instructions": DECLARATION})), 1)
        self.assertEqual(memory.bind_tools({"prompt_cache_key": "text-only", "input": "hello"}), [])


if __name__ == "__main__":
    unittest.main()
