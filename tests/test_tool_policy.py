"""Tool-selection and executor-availability regressions from the desktop session."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bps_proxy.server import Handler
from bps_proxy.tool_policy import ToolSelectionError, missing_call_message, missing_call_reason
from bps_proxy.wire import (EXECUTION_GUIDANCE, CallMemory, iter_client_tools,
                            prepare_body, protocol_instructions, translate_transport)
from tests.test_forward import assistant, completed, office, transport
from tests.test_native_custom import DECLARATION, EXEC

DENIAL = "当前会话没有本地文件执行工具，无法直接替你打包。"
CODE_MODE = {"input": "打包项目并验证 ZIP", "instructions": DECLARATION}
FUNCTION = {"type": "function", "name": "read_file", "description": "Read a file.", "parameters": {}}
COMMAND_DECLARATION = DECLARATION + """
declare namespace tools {
    function exec_command(args: {
        cmd: string;
        workdir?: string;
        max_output_tokens?: number;
    }): Promise<unknown>;
}
"""


def exec_call(call_id="call_exec"):
    return transport(call_id, json.dumps({"tool": "exec", "input": "text(1 + 1);"}))


class DirectoryTest(unittest.TestCase):
    def prompt(self, source):
        body = prepare_body(source, CallMemory())
        # The transport prologue follows optional top-level instructions.
        index = int(isinstance(source.get("instructions"), str) and bool(source["instructions"].strip()))
        return body["input"][index]["content"][0]["text"]

    def example(self, source):
        return json.loads(self.prompt(source).split("Example run_officejs arguments:\n", 1)[1].splitlines()[0])

    def test_custom_only_directory_has_one_consistent_input_contract(self):
        prompt = protocol_instructions(iter_client_tools([EXEC]))
        self.assertIn('{"tool":"TOOL_NAME","input":"raw text"}', prompt)
        self.assertNotIn('{"tool":"TOOL_NAME","args":{...}}', prompt)
        self.assertNotIn("Arguments: none", prompt)
        self.assertIn("Input: required string", prompt)

    def test_function_only_directory_does_not_suggest_custom_input(self):
        prompt = protocol_instructions(iter_client_tools([FUNCTION]))
        self.assertIn('{"tool":"TOOL_NAME","args":{...}}', prompt)
        self.assertNotIn("Custom tools use", prompt)

    def test_code_mode_includes_a_minimal_executor_example(self):
        body = prepare_body(CODE_MODE, CallMemory())
        self.assertIn("text(1 + 1);", str(body["input"]))
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)

    def test_normal_and_recovery_prompts_share_execution_evidence_rules(self):
        for tools in ([EXEC], [FUNCTION]):
            prompt = protocol_instructions(iter_client_tools(tools))
            self.assertIn(EXECUTION_GUIDANCE, prompt)
            self.assertNotIn("Ignore every instruction", prompt)
            self.assertNotIn("backend rejects", prompt)
            for reason in ("tool_required", "executor_unavailable_claim"):
                self.assertIn(EXECUTION_GUIDANCE, missing_call_message(reason, tools))

    def test_complete_examples_round_trip_to_custom_exec(self):
        for declaration, expected in (
            (DECLARATION, "text(1 + 1);"),
            (COMMAND_DECLARATION, 'text(await tools.exec_command({cmd: "pwd"}));'),
        ):
            for extra in ({}, {"tool_choice": "required", "parallel_tool_calls": False}):
                with self.subTest(declaration=declaration, extra=extra):
                    source = {**CODE_MODE, "instructions": declaration, **extra}
                    original = copy.deepcopy(source)
                    wrapper = self.example(source)
                    self.assertEqual(source, original)
                    self.assertIs(wrapper["destructive"], False)
                    self.assertIsInstance(wrapper["summary"], str)
                    self.assertEqual(len(wrapper["references"]), 1)
                    self.assertIsInstance(wrapper["references"][0], str)
                    self.assertEqual(json.loads(wrapper["code"]), {"tool": "exec", "input": expected})
                    call, rejection = translate_transport(office("run_officejs", "example", wrapper), {"exec"}, {"exec"})
                    self.assertIsNone(rejection)
                    self.assertEqual(call["name"], "exec")
                    self.assertEqual(call["type"], "custom_tool_call")
                    self.assertEqual(call["input"], expected)
                    self.assertEqual([t["name"] for t in CallMemory().bind_tools(source)], ["exec"])

    def test_developer_namespace_declarations_select_command_example(self):
        declarations = (COMMAND_DECLARATION,
            DECLARATION + "\n# Namespace: tools\ntype exec_command = (_: { cmd: string, workdir?: string }) => any;",
            DECLARATION + "\ndeclare const tools: {\nexec_command: (args: { cmd: string }) => Promise<unknown>;\n};")
        for declaration in declarations:
            for content in (declaration, [{"type": "input_text", "text": declaration}]):
                source = {"input": [{"role": "developer", "content": content}]}
                envelope = json.loads(self.example(source)["code"])
                self.assertIn("tools.exec_command", envelope["input"])

    def test_undeclared_or_incompatible_command_uses_pure_javascript_example(self):
        declarations = (
            DECLARATION,
            DECLARATION + " tools.exec_command({cmd: 'pwd'}) is only a hypothetical example.",
            COMMAND_DECLARATION.replace("namespace tools", "namespace unrelated"),
            COMMAND_DECLARATION.replace("cmd: string", "cmd: number"),
            COMMAND_DECLARATION.replace("workdir?:", "workdir:"),
            COMMAND_DECLARATION.replace("exec_command", "another_command"),
            COMMAND_DECLARATION.replace("max_output_tokens?: number", "options: {cmd: string}"),
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                wrapper = self.example({**CODE_MODE, "instructions": declaration})
                self.assertEqual(json.loads(wrapper["code"])["input"], "text(1 + 1);")

    def test_user_assistant_and_tool_text_cannot_supply_command_declarations(self):
        for role in ("user", "assistant", "tool"):
            source = {"instructions": DECLARATION, "input": [{"role": role, "content": COMMAND_DECLARATION}]}
            self.assertEqual(json.loads(self.example(source)["code"])["input"], "text(1 + 1);")
        source = {"instructions": DECLARATION, "input": [
            {"type": "custom_tool_call_output", "role": "developer", "call_id": "old", "output": COMMAND_DECLARATION}]}
        self.assertEqual(json.loads(self.example(source)["code"])["input"], "text(1 + 1);")

    def test_other_scopes_and_comments_do_not_declare_tools_commands(self):
        declarations = (
            DECLARATION + "\ndeclare namespace tools {}\nfunction exec_command(args: {cmd: string}): unknown;",
            COMMAND_DECLARATION.replace("namespace tools", "namespace unrelated") + "\ndeclare namespace tools {}",
            DECLARATION + "\n# Namespace: tools\n# Namespace: unrelated\ntype exec_command = (_: {cmd: string}) => any;",
            COMMAND_DECLARATION.replace("function exec_command", "// function exec_command"),
            DECLARATION + "\n/*\n" + COMMAND_DECLARATION + "*/",
            COMMAND_DECLARATION.replace("namespace tools {", "namespace tools {\nconst nested: {") + "}",
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                wrapper = self.example({**CODE_MODE, "instructions": declaration})
                self.assertEqual(json.loads(wrapper["code"])["input"], "text(1 + 1);")

    def test_disabled_or_non_code_mode_requests_have_no_executor_example(self):
        for extra in ({"tools": []}, {"tools": [FUNCTION]}, {"tool_choice": "none"}):
            prompt = self.prompt({**CODE_MODE, "instructions": COMMAND_DECLARATION, **extra})
            self.assertNotIn("Example run_officejs arguments:", prompt)
            if extra.get("tools") != [FUNCTION]:
                self.assertIn("No client tools are enabled", prompt)
        self.assertNotIn("Example run_officejs arguments:", self.prompt({"input": "Explain tools", "tools": [EXEC]}))

    def test_none_disables_cached_exec_without_breaking_legacy_continuations(self):
        memory = CallMemory()
        source = {**CODE_MODE, "prompt_cache_key": "same"}
        self.assertEqual([t["name"] for t in memory.bind_tools(source)], ["exec"])
        self.assertEqual(memory.bind_tools({**source, "tool_choice": "none"}), [])
        self.assertEqual([t["name"] for t in memory.bind_tools({**source, "tools": []})], ["exec"])
        self.assertEqual([t["name"] for t in memory.bind_tools(source)], ["exec"])
        self.assertEqual(memory.bind_tools({**source, "tools": [FUNCTION]}), [FUNCTION])


class ClaimTest(unittest.TestCase):
    def test_real_session_claims_and_english_are_detected(self):
        claims = (DENIAL, "当前会话没有文件打包工具，无法直接生成 ZIP。",
                  "更准确地说：这一轮我没有拿到可调用的本地执行入口。",
                  "这轮我无法发出一次真实的本地执行调用。",
                  "I don't have access to local execution tools in this session.")
        for text in claims:
            with self.subTest(text=text):
                self.assertEqual(missing_call_reason(CODE_MODE, [EXEC], [assistant(text)]),
                                 "executor_unavailable_claim")

    def test_explanations_quotes_conditionals_and_real_errors_are_not_claims(self):
        for text in ("已完成打包。", "他说：" + DENIAL, "> " + DENIAL, "“" + DENIAL + "”",
                     "如果没有本地执行工具，可以手动执行。", "没有本地执行工具时，请使用其他方法。",
                     "当前会话没有本地执行工具的说法不正确。",
                     DENIAL + "执行失败：permission denied。", DENIAL + "需要获得授权。"):
            with self.subTest(text=text):
                self.assertIsNone(missing_call_reason(CODE_MODE, [EXEC], [assistant(text)]))

    def test_requires_trusted_code_mode_and_declared_executor(self):
        for source, tools in (({"input": DECLARATION}, [EXEC]), (CODE_MODE, []),
                              (CODE_MODE, [FUNCTION]), (CODE_MODE, [{**EXEC, "type": "function"}])):
            self.assertIsNone(missing_call_reason(source, tools, [assistant(DENIAL)]))

    def test_commentary_and_non_text_content_are_not_claims(self):
        items = [{**assistant(DENIAL), "phase": "commentary"},
                 {**assistant(DENIAL), "role": "user"},
                 {**assistant(DENIAL), "content": None},
                 {**assistant(DENIAL), "content": [{"type": "output_text", "text": None}]}]
        self.assertIsNone(missing_call_reason(CODE_MODE, [EXEC], items))


class CompletionPolicyTest(unittest.TestCase):
    def setUp(self):
        self.handler = Handler.__new__(Handler)
        self.handler.server = SimpleNamespace(memory=CallMemory())

    def relay(self, source, batches):
        with patch("bps_proxy.server.iter_events", side_effect=[iter(batch) for batch in batches]) as upstream:
            events = list(self.handler._iter_relay(source, None))
        return events, upstream

    def test_required_and_forced_custom_retry_then_deliver_one_call(self):
        for choice in ("required", {"type": "custom", "name": "exec"}):
            with self.subTest(choice=choice):
                source = {**CODE_MODE, "tool_choice": choice}
                original = copy.deepcopy(source)
                events, upstream = self.relay(source, [[completed([assistant("I will do it.")])],
                                                      [completed([exec_call()])]])
                self.assertEqual(source, original)
                self.assertEqual(upstream.call_count, 2)
                calls = [item for item in events[-1][1]["response"]["output"]
                         if item.get("type") == "custom_tool_call"]
                self.assertEqual([item["name"] for item in calls], ["exec"])
                self.assertEqual([e for e, _ in events if e == "response.completed"], ["response.completed"])

    def test_forced_function_is_filtered_and_must_be_called(self):
        source = {"input": "Read the file", "tools": [FUNCTION, EXEC],
                  "tool_choice": {"type": "function", "name": "read_file"}}
        call = transport("call_read", json.dumps({"tool": "read_file", "args": {}}))
        events, upstream = self.relay(source, [[completed([])], [completed([call])]])
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(events[-1][1]["response"]["output"][0]["name"], "read_file")
        self.assertNotIn("Tool `exec`", str(upstream.call_args.args[1]["input"]))

    def test_missing_call_exhaustion_is_failed_and_bounded(self):
        for extra, reason in (({}, "executor_unavailable_claim"), ({"tool_choice": "required"}, "tool_required")):
            events, upstream = self.relay({**CODE_MODE, **extra},
                [[completed([assistant(DENIAL, "msg_first")])], [completed([assistant(DENIAL, "msg_last")])]])
            self.assertEqual(upstream.call_count, 2)
            self.assertEqual(events[-1][0], "response.failed")
            self.assertEqual(events[-1][1]["response"]["error"]["code"], reason)
            self.assertNotIn("response.completed", [name for name, _ in events])
            self.assertEqual([item["id"] for item in events[-1][1]["response"]["output"]],
                             ["msg_first", "msg_last"])

    def test_required_without_declared_or_matching_tools_fails_before_upstream(self):
        for source in ({"input": "Run", "tool_choice": "required"},
                       {**CODE_MODE, "tools": [], "tool_choice": "required"},
                       {**CODE_MODE, "tool_choice": {"type": "custom", "name": "missing"}}):
            with patch("bps_proxy.server.iter_events") as upstream:
                with self.assertRaises(ToolSelectionError):
                    list(self.handler._iter_relay(source, None))
            upstream.assert_not_called()

    def test_none_empty_and_plain_qa_do_not_retry(self):
        for source, text in (({**CODE_MODE, "tool_choice": "none"}, DENIAL),
                             ({**CODE_MODE, "tools": []}, DENIAL),
                             ({"input": "Explain tools"}, DENIAL),
                             (CODE_MODE, "2 + 2 = 4")):
            self.handler.server.memory = CallMemory()
            events, upstream = self.relay(source, [[completed([assistant(text)])]])
            self.assertEqual(upstream.call_count, 1)
            self.assertEqual(events[-1][0], "response.completed")

    def test_auto_correction_can_finish_with_an_explanation(self):
        events, upstream = self.relay(CODE_MODE, [[completed([assistant(DENIAL)])],
            [completed([assistant("执行器已声明，这个问题不需要执行操作。", "msg_fixed")])]])
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(events[-1][0], "response.completed")

    def test_a_delivered_call_prevents_retry_even_with_a_denial(self):
        for extra in ({}, {"tool_choice": "required"}):
            events, upstream = self.relay({**CODE_MODE, **extra},
                [[completed([assistant(DENIAL), exec_call()])]])
            self.assertEqual(upstream.call_count, 1)
            self.assertEqual(sum(e == "response.custom_tool_call_input.done" for e, _ in events), 1)

    def test_refusal_and_incomplete_or_failed_responses_are_not_retried(self):
        refusal = {**assistant(""), "content": [{"type": "refusal", "refusal": "Cannot help with that request."}]}
        events, upstream = self.relay({**CODE_MODE, "tool_choice": "required"}, [[completed([refusal])]])
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(events[-1][1]["response"]["output"], [refusal])
        for state in ("incomplete", "failed"):
            event, payload = completed([assistant(DENIAL)])
            payload["response"]["status"] = state
            events, upstream = self.relay({**CODE_MODE, "tool_choice": "required"}, [[("response." + state, payload)]])
            self.assertEqual(upstream.call_count, 1)
            self.assertEqual(events[-1][0], "response." + state)

    def test_stream_retry_keeps_order_identity_history_and_replay_iteration(self):
        denial = assistant(DENIAL, "msg_denial")
        native = exec_call()
        first = [("response.created", {"response": {"id": "resp_first"}}),
                 ("response.output_item.added", {"output_index": 0, "item": denial}),
                 ("response.output_text.delta", {"output_index": 0, "delta": DENIAL, "item_id": "msg_denial"}),
                 ("response.output_item.done", {"output_index": 0, "item": denial}), completed([denial])]
        second = [("response.created", {"response": {"id": "resp_second"}}), completed([native])]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "calls.json"
            self.handler.server.memory = CallMemory(state)
            events, upstream = self.relay(CODE_MODE, [first, second])
            self.assertEqual(sum(name == "response.created" for name, _ in events), 1)
            self.assertEqual([p["sequence_number"] for _, p in events], list(range(len(events))))
            self.assertEqual([p["output_index"] for name, p in events if name == "response.output_item.added"], [0, 1])
            self.assertEqual({p["response"]["id"] for _, p in events if "response" in p}, {"resp_first"})
            metadata = [call.args[1]["metadata"] for call in upstream.call_args_list]
            retry_input = upstream.call_args_list[1].args[1]["input"]
            self.assertIn({k: v for k, v in denial.items() if k != "id"}, retry_input)
            self.assertIn("do not repeat operations", retry_input[-1]["content"][0]["text"])
            self.assertEqual(self.handler.server.memory.recall("call_exec"), native)
            call = events[-1][1]["response"]["output"][-1]
            self.handler.server.memory = CallMemory(state)
            source = {**CODE_MODE, "input": [{"role": "user", "content": CODE_MODE["input"]}, call,
                {"type": "custom_tool_call_output", "call_id": call["call_id"], "output": "2"}]}
            _, follow = self.relay(source, [[completed([assistant("Done.")])]])
            metadata.append(follow.call_args.args[1]["metadata"])
            self.assertEqual(len({m["turn_id"] for m in metadata}), 1)
            self.assertEqual([m["agent_iteration"] for m in metadata], ["1", "2", "3"])

    def test_office_and_missing_call_retries_share_a_finite_total_budget(self):
        bad = office("read_ranges", "call_bad", {})
        batches = [[completed([bad])], [completed([assistant(DENIAL)])],
                   [completed([bad])], [completed([bad])], [completed([bad])]]
        events, upstream = self.relay(CODE_MODE, batches)
        self.assertEqual(upstream.call_count, 5)
        self.assertEqual(events[-1][0], "response.failed")

    def test_recovery_logs_counts_and_reasons_without_text_or_script(self):
        with self.assertLogs("bps_proxy", "INFO") as logs:
            self.relay(CODE_MODE, [[completed([assistant(DENIAL + "private-response")])],
                                  [completed([exec_call()])]])
        text = chr(10).join(logs.output)
        self.assertIn("client_calls=0", text)
        self.assertIn("client_calls=1", text)
        self.assertIn("reason=executor_unavailable_claim", text)
        for secret in (DENIAL, "private-response", "text(1 + 1)", CODE_MODE["input"]):
            self.assertNotIn(secret, text)

    def test_corrected_call_writes_once_and_replays_the_actual_file_result(self):
        tool = {"type": "function", "name": "write_fixture", "description": "Write the fixture.",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                               "required": ["text"]}}
        source = {"input": "Write and verify the fixture.", "tools": [tool], "tool_choice": "required"}
        native = transport("call_file", json.dumps({"tool": "write_fixture", "args": {"text": "verified recovery"}}))
        events, upstream = self.relay(source, [[completed([])], [completed([native])]])
        calls = [item for item in events[-1][1]["response"]["output"] if item["type"] == "function_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(upstream.call_count, 2)
        call = calls[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.txt"
            path.write_text(json.loads(call["arguments"])["text"])
            actual = path.read_text()
            self.assertEqual(actual, "verified recovery")
            result = {"type": "function_call_output", "call_id": call["call_id"], "output": actual}
            follow = {**source, "tool_choice": "auto", "input": [
                {"role": "user", "content": source["input"]}, call, result]}
            final, replay = self.relay(follow, [[completed([assistant("Verified.")])]])
            self.assertEqual(replay.call_count, 1)
            self.assertIn(native, replay.call_args.args[1]["input"])
            self.assertIn(result, replay.call_args.args[1]["input"])
            self.assertFalse(any(item["type"] == "function_call" for item in final[-1][1]["response"]["output"]))
            self.assertEqual(path.read_text(), actual)
