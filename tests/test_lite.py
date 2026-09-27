import copy
import json
import unittest

from bps_proxy.wire import CallMemory, StreamRewriter, prepare_body

EXEC = {'type': 'custom', 'name': 'exec', 'description': 'Execute JavaScript.'}
FUNCTION = {'type': 'function', 'name': 'read_file', 'parameters': {
    'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}}


def source(tools=None, **extra):
    return {'prompt_cache_key': 'lite-thread', 'input': [
        {'type': 'additional_tools', 'id': 'at_1', 'role': 'developer',
         'tools': [EXEC] if tools is None else tools},
        {'role': 'user', 'content': 'Read a file.'}], **extra}


class LiteToolsTest(unittest.TestCase):
    def test_cold_cache_binds_structured_tools_for_every_model(self):
        for model in ('gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-5.6-terra'):
            with self.subTest(model=model):
                request = source(model=model)
                original = copy.deepcopy(request)
                memory = CallMemory()
                self.assertEqual([t['name'] for t in memory.bind_tools(request)], ['exec'])
                body = prepare_body(request, memory)
                self.assertNotIn('tools', body)
                self.assertFalse(any(i.get('type') == 'additional_tools' for i in body['input']))
                self.assertIn('Tool '+chr(96)+'exec'+chr(96), body['input'][0]['content'][0]['text'])
                self.assertEqual(request, original)

    def test_namespaces_and_custom_format_survive(self):
        grammar = {'type': 'grammar', 'syntax': 'lark', 'definition': 'start: /[a-z]+/'}
        request = source([{'type': 'namespace', 'name': 'functions', 'tools': [
            {**EXEC, 'format': grammar}, FUNCTION]}])
        tools = CallMemory().bind_tools(request)
        self.assertEqual([t['name'] for t in tools], ['exec', 'read_file'])
        self.assertEqual(tools[0]['format'], grammar)
        self.assertEqual(tools[1]['parameters'], FUNCTION['parameters'])
        self.assertIn(grammar['definition'], prepare_body(request, CallMemory())['input'][0]['content'][0]['text'])

    def test_none_disables_this_request_and_followup_retains_catalog(self):
        memory = CallMemory()
        self.assertEqual(memory.bind_tools(source(tool_choice='none')), [])
        self.assertEqual([t['name'] for t in memory.bind_tools({
            'prompt_cache_key': 'lite-thread', 'input': 'continue'})], ['exec'])

    def test_empty_lite_declaration_revokes_previous_tools(self):
        memory = CallMemory()
        memory.bind_tools(source())
        self.assertEqual(memory.bind_tools(source([])), [])

    def test_untrusted_and_malformed_structured_declarations_fail(self):
        for patch in ({'role': 'user'}, {'role': 'assistant'}, {'role': 'tool'},
                      {'tools': {}}, {'tools': [False]}, {'tools': [{'type': 'custom'}]}):
            with self.subTest(patch=patch):
                request = source()
                request['input'][0].update(patch)
                with self.assertRaises(ValueError):
                    prepare_body(request, CallMemory())

    def test_conflicts_fail_and_identical_duplicates_are_deduplicated(self):
        request = source(tools=[EXEC])
        request['tools'] = [{**EXEC, 'type': 'function'}]
        with self.assertRaises(ValueError):
            CallMemory().bind_tools(request)
        request['tools'] = [EXEC]
        self.assertEqual(len(CallMemory().bind_tools(request)), 1)

    def test_catalog_does_not_cross_account_or_conversation(self):
        memory = CallMemory()
        memory.scoped('account-a').bind_tools(source())
        followup = {'prompt_cache_key': 'lite-thread', 'input': 'continue'}
        self.assertEqual(memory.scoped('account-b').bind_tools(followup), [])
        self.assertEqual(memory.scoped('account-a').bind_tools({
            **followup, 'prompt_cache_key': 'other-thread'}), [])

    def test_lite_native_custom_call_and_full_result_replay(self):
        memory = CallMemory()
        request = source()
        body = prepare_body(request, memory)
        call = {'type': 'custom_tool_call', 'id': 'ctc_exec', 'call_id': 'call_exec',
                'name': 'exec', 'input': 'text(1 + 1);', 'status': 'completed'}
        rewriter = StreamRewriter(memory.bind_tools(request), memory,
                                  turn_id=body['metadata']['turn_id'], iteration=1)
        events = rewriter.handle('response.completed', {'response': {
            'id': 'resp_1', 'status': 'completed', 'output': [call]}})
        self.assertEqual(events[-1][1]['response']['output'], [call])
        request['input'].extend([call, {'type': 'custom_tool_call_output',
                                      'call_id': 'call_exec', 'output': '2'}])
        followup = prepare_body(request, memory)
        self.assertEqual(followup['metadata']['turn_id'], body['metadata']['turn_id'])
        self.assertEqual(followup['metadata']['agent_iteration'], '2')
        self.assertIn(call, followup['input'])
