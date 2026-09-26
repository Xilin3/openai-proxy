"""Regression coverage for the proxy's original transport contract."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bps_proxy.server import Handler
from bps_proxy.wire import CallMemory, StreamRewriter, prepare_body, protocol_instructions, iter_client_tools, translate_input


TOOLS = [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}]


def native_call(name="run_officejs", call_id="call_weather"):
    return {"type": "function_call", "id": "fc_" + call_id, "call_id": call_id, "name": name,
            "arguments": {"summary": "Look up weather", "references": ["Tokyo"], "destructive": False,
                          "code": json.dumps({"tool": "get_weather", "args": {"city": "Tokyo"}})}}


def completed(output):
    return "response.completed", {"type": "response.completed", "response": {"id": "resp_test", "output": output}}


class TransportContractTest(unittest.TestCase):
    def test_tool_directory_uses_descriptions_not_serialized_schemas(self):
        schema = {"type": "object", "properties": {"mode": {"type": "string", "enum": ["read", "write"]}},
                  "required": ["mode"], "additionalProperties": False}
        text = protocol_instructions(iter_client_tools([{"type": "function", "name": "example", "parameters": schema}]))
        self.assertNotIn(json.dumps(schema, ensure_ascii=False, separators=(",", ":")), text)
        self.assertIn("read", text)
        self.assertIn("write", text)

    def test_string_input_keeps_turn_when_expanded_for_continuation(self):
        memory = CallMemory()
        first = prepare_body({"input": "hi"}, memory)
        continued = prepare_body({"input": [
            {"role": "user", "content": "hi"},
            native_call("update_plan", "plan"),
            {"type": "function_call_output", "call_id": "plan", "output": "ok"},
        ]}, memory)
        self.assertEqual(first["metadata"]["task_id"], continued["metadata"]["task_id"])
        self.assertEqual(first["metadata"]["turn_id"], continued["metadata"]["turn_id"])

    def test_complete_native_item_is_preserved_without_mutating_events(self):
        memory = CallMemory()
        native = native_call()
        original = json.loads(json.dumps(native))
        rewriter = StreamRewriter(iter_client_tools(TOOLS), memory)
        rewriter.handle("response.output_item.done", {"output_index": 0, "item": native})
        self.assertEqual(native, original)
        self.assertIsNone(memory.recall(native['call_id']))
        rewriter.handle(*completed([native]))
        self.assertEqual(memory.recall(native["call_id"]), original)

    def test_output_only_replay_restores_complete_cached_item(self):
        memory = CallMemory()
        native = native_call()
        memory.remember(native)
        output = {"type": "function_call_output", "call_id": native["call_id"], "output": "18 C"}
        replay = translate_input([output], memory)
        self.assertEqual(replay, [native, output])

    def test_terminal_response_keeps_final_native_metadata(self):
        memory = CallMemory()
        rewriter = StreamRewriter(iter_client_tools(TOOLS), memory)
        native = native_call()
        rewriter.handle("response.output_item.done", {"output_index": 0, "item": native})
        final = json.loads(json.dumps(native))
        final["arguments"]["references"].append("final reference")
        final["arguments"]["summary"] = "Final summary"
        final["status"] = "completed"
        rewriter.handle(*completed([final]))
        self.assertEqual(memory.recall(final["call_id"]), final)

    def test_new_user_turn_gets_new_identity(self):
        memory = CallMemory()
        first = prepare_body({"input": [{"role": "user", "content": "hi"}]}, memory)
        following = prepare_body({"input": [
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "next question"},
        ]}, memory)
        self.assertEqual(first["metadata"]["task_id"], following["metadata"]["task_id"])
        self.assertNotEqual(first["metadata"]["turn_id"], following["metadata"]["turn_id"])
        self.assertEqual(following["metadata"]["agent_iteration"], "1")

    def test_iteration_retries_do_not_skip_and_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.json"
            memory = CallMemory(path, limit=2)
            with ThreadPoolExecutor(max_workers=4) as executor:
                values = list(executor.map(lambda _: memory.advance_turn("same-turn", 1), range(12)))
            self.assertEqual(set(values), {1})
            self.assertEqual(memory.advance_turn("same-turn", 1), 1)
            self.assertEqual(memory.advance_turn("same-turn", 3), 3)
            self.assertEqual(memory.advance_turn("same-turn", 3, bump=True), 4)
            self.assertEqual(CallMemory(path, limit=2).advance_turn("same-turn", 3), 4)
            memory.advance_turn("second-turn", 1)
            memory.advance_turn("third-turn", 1)
            stored = json.loads(path.read_text())
            self.assertEqual(set(stored["iterations"]), {"second-turn", "third-turn"})

    def test_legacy_cache_without_iteration_state_remains_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.json"
            native = native_call()
            path.write_text(json.dumps({"items": {native["call_id"]: native}}))
            memory = CallMemory(path)
            self.assertEqual(memory.recall(native["call_id"]), native)
            self.assertEqual(memory.advance_turn("legacy-turn", minimum=7), 7)

    def test_nested_constraints_are_described(self):
        schema = {"type": "object", "$defs": {"city": {"type": "string", "minLength": 1}},
                  "properties": {"locations": {"type": "array", "minItems": 1,
                                  "items": {"anyOf": [{"$ref": "#/$defs/city"}, {"type": "null"}]}}}}
        text = protocol_instructions(iter_client_tools([
            {"type": "function", "name": "weather", "description": "Look up a city.", "parameters": schema}
        ]))
        self.assertIn("Look up a city.", text)
        self.assertIn("- locations (", text)
        self.assertNotIn("#/$defs/city", text)
        self.assertNotIn("minLength", text)
        self.assertNotIn('"properties":', text)

    def test_hidden_hops_do_not_reset_iteration_on_next_client_request(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "calls.json"
            handler = Handler.__new__(Handler)
            handler.server = SimpleNamespace(memory=CallMemory(state))
            source = {"input": "hi", "tools": TOOLS}
            batches = [[completed([native_call("update_plan", "plan")])], [completed([native_call()])]]
            with patch("bps_proxy.server.iter_events", side_effect=[iter(b) for b in batches]) as upstream:
                events = list(handler._iter_relay(source, None))
            metadata = [call.args[1]["metadata"] for call in upstream.call_args_list]
            client_call = events[-1][1]["response"]["output"][0]
            handler.server.memory = CallMemory(state)
            source["input"] = [{"role": "user", "content": "hi"}, client_call,
                               {"type": "function_call_output", "call_id": client_call["call_id"], "output": "18 C"}]
            with patch("bps_proxy.server.iter_events", return_value=iter([completed([])])) as upstream:
                list(handler._iter_relay(source, None))
            metadata.append(upstream.call_args.args[1]["metadata"])
            self.assertEqual(len({item["turn_id"] for item in metadata}), 1)
            self.assertEqual([item["agent_iteration"] for item in metadata], ["1", "2", "3"])

    def test_upstream_body_never_contains_client_tool_fields(self):
        for choice in ("auto", "required", "none", {"type": "function", "name": "get_weather"}):
            with self.subTest(choice=choice):
                body = prepare_body({"input": "hi", "tools": TOOLS, "tool_choice": choice}, CallMemory())
                self.assertNotIn("tools", body)
                self.assertNotIn("tool_choice", body)


if __name__ == "__main__":
    unittest.main()
