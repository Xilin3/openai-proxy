import copy
import gzip
import http.client
import json
import threading
import unittest
import zlib
from unittest.mock import patch

import zstandard

from bps_proxy.compaction import compact_request
from bps_proxy.server import ProxyServer
from bps_proxy.wire import CallMemory, append_input, model_catalog, prepare_body


OPAQUE = {'id': 'cmp_test', 'type': 'compaction', 'encrypted_content': 'opaque-content-not-a-summary'}


def completion(output, status='completed'):
    return 'response.' + status, {'type': 'response.' + status, 'response': {
        'id': 'resp_test', 'object': 'response', 'created_at': 1790467200,
        'status': status, 'output': output, 'usage': {'input_tokens': 40, 'output_tokens': 10, 'total_tokens': 50}}}


class CompatibilityHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ProxyServer(('127.0.0.1', 0), CallMemory())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.server.memory = CallMemory()
        self.sent = []
        def upstream(session, body):
            self.sent.append(copy.deepcopy(body))
            return iter([completion([copy.deepcopy(OPAQUE)])])
        self.auth = patch('bps_proxy.server.load_session', return_value=None)
        self.upstream = patch('bps_proxy.server.iter_events', side_effect=upstream)
        self.auth.start()
        self.upstream.start()
        self.addCleanup(self.auth.stop)
        self.addCleanup(self.upstream.stop)

    def request(self, path='/v1/responses/compact', body=None, headers=None, method='POST'):
        if body is None and method == 'POST':
            body = {'input': [{'role': 'user', 'content': 'remember this'}], 'prompt_cache_key': 'test-thread'}
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_compact_paths_return_native_payload(self):
        for path in ('/responses/compact', '/v1/responses/compact', '/v1/responses/compact/?test=1'):
            with self.subTest(path=path):
                status, raw = self.request(path)
                self.assertEqual(status, 200)
                result = json.loads(raw)
                self.assertEqual(result['object'], 'response.compaction')
                self.assertEqual(result['output'], [OPAQUE])
                self.assertEqual(result['usage']['total_tokens'], 50)
                self.assertEqual(self.sent[-1]['input'][-1], {'type': 'compaction_trigger'})
                self.assertNotIn('tools', self.sent[-1])
                self.assertNotIn('tool_choice', self.sent[-1])

    def test_compaction_payload_survives_followup_and_tools_remain_enabled(self):
        source = {'prompt_cache_key': 'test-thread', 'input': 'remember this',
                  'tools': [{'type': 'function', 'name': 'read_file', 'parameters': {'type': 'object'}}]}
        self.server.memory.bind_tools(source)
        status, raw = self.request(body=source)
        self.assertEqual(status, 200)
        followup = {**source, 'input': json.loads(raw)['output'] + [{'role': 'user', 'content': 'continue'}]}
        del followup['tools']
        status, _ = self.request('/v1/responses', followup)
        self.assertEqual(status, 200)
        self.assertIn(OPAQUE, self.sent[-1]['input'])
        self.assertIn('read_file', self.sent[-1]['input'][0]['content'][0]['text'])

    def test_first_compact_request_can_seed_legacy_tool_catalog(self):
        source = {'prompt_cache_key': 'first-compact', 'input': 'remember this',
                  'tools': [{'type': 'function', 'name': 'read_file', 'parameters': {'type': 'object'}}]}
        status, _ = self.request(body=source)
        self.assertEqual(status, 200)
        self.assertNotIn('run_officejs', json.dumps(self.sent[-1]))
        followup = {'prompt_cache_key': 'first-compact', 'input': 'continue'}
        self.assertEqual(self.server.memory.bind_tools(followup)[0]['name'], 'read_file')

    def test_compact_plain_text_is_not_reported_as_success(self):
        plain = {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'summary'}]}
        with patch('bps_proxy.server.iter_events', return_value=iter([completion([plain])])) as upstream:
            status, raw = self.request()
        self.assertEqual(status, 502)
        self.assertNotIn('output', json.loads(raw))
        upstream.assert_called_once()

    def test_compact_failure_and_empty_ciphertext_do_not_succeed(self):
        for output, state in (([OPAQUE], 'failed'), ([OPAQUE], 'incomplete'),
                              ([{**OPAQUE, 'encrypted_content': ''}], 'completed')):
            with self.subTest(state=state), patch('bps_proxy.server.iter_events', return_value=iter([completion(output, state)])):
                status, _ = self.request()
                self.assertEqual(status, 502)

    def test_compact_does_not_retry_office_tools(self):
        call = {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1', 'name': 'read_ranges', 'arguments': '{}'}
        with patch('bps_proxy.server.iter_events', return_value=iter([completion([call, OPAQUE])])) as upstream:
            status, _ = self.request()
        self.assertEqual(status, 502)
        upstream.assert_called_once()

    def test_multiple_native_compactions_are_rejected(self):
        for path in ('/v1/responses', '/v1/responses/compact'):
            with self.subTest(path=path), patch('bps_proxy.server.iter_events',
                    return_value=iter([completion([OPAQUE, {**OPAQUE, 'id': 'cmp_second'}])])):
                status, _ = self.request(path, {'input': [{'type': 'compaction_trigger'}]})
                self.assertEqual(status, 502)

    def test_compaction_done_events_must_match_terminal(self):
        for items, valid in (([OPAQUE], True), ([OPAQUE, OPAQUE], False),
                             ([{**OPAQUE, 'encrypted_content': 'different'}], False)):
            events = [('response.output_item.done', {'output_index': 0, 'item': item}) for item in items]
            events.append(completion([OPAQUE]))
            with self.subTest(items=items), patch('bps_proxy.server.iter_events', return_value=iter(events)):
                status, raw = self.request(body={'input': 'remember', 'stream': True})
                if valid:
                    self.assertEqual(status, 200)
                    self.assertEqual(raw.count(b'event: response.output_item.done'), 1)
                    self.assertIn(b'event: response.completed', raw)
                else:
                    self.assertEqual(status, 502)
                    self.assertNotIn(b'event: response.completed', raw)

    def test_compaction_may_include_other_output_items(self):
        message = {'type': 'message', 'role': 'assistant', 'content': []}
        with patch('bps_proxy.server.iter_events', return_value=iter([completion([message, OPAQUE])])):
            status, raw = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['output'], [message, OPAQUE])

    def test_compaction_event_buffer_is_bounded(self):
        events = [('response.output_text.delta', {'delta': 'x' * 100})] * 3
        events.append(completion([OPAQUE]))
        for setting, limit in (('MAX_COMPACT_EVENTS', 2), ('MAX_COMPACT_EVENT_BYTES', 50)):
            with self.subTest(setting=setting), patch('bps_proxy.server.' + setting, limit), patch(
                    'bps_proxy.server.iter_events', return_value=iter(events)):
                status, raw = self.request(body={'input': 'remember', 'stream': True})
                self.assertEqual(status, 200)
                self.assertIn(b'event: response.failed', raw)
                self.assertNotIn(b'event: response.completed', raw)

    def test_native_responses_compaction_uses_same_validation(self):
        source = {'input': [{'role': 'user', 'content': 'remember'}, {'type': 'compaction_trigger'}]}
        status, raw = self.request('/v1/responses', source)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['object'], 'response')
        self.assertEqual(json.loads(raw)['output'], [OPAQUE])
        with patch('bps_proxy.server.iter_events', return_value=iter([completion([])])) as upstream:
            status, _ = self.request('/v1/responses', source)
        self.assertEqual(status, 502)
        upstream.assert_called_once()

    def test_compact_truncated_stream_is_an_error(self):
        with patch('bps_proxy.server.iter_events', return_value=iter([])):
            status, _ = self.request()
        self.assertEqual(status, 502)

    def test_invalid_trigger_placement_fails_before_auth(self):
        for path in ('/v1/responses', '/v1/responses/compact'):
            source = {'input': [{'type': 'compaction_trigger'}, {'role': 'user', 'content': 'hi'}]}
            with patch('bps_proxy.server.load_session') as auth:
                status, _ = self.request(path, source)
            self.assertEqual(status, 400)
            auth.assert_not_called()

    def test_streaming_invalid_compaction_has_no_completed_terminal(self):
        events = [('response.created', {'type': 'response.created', 'response': {
            'id': 'resp_test', 'status': 'in_progress', 'output': []}}), completion([])]
        with patch('bps_proxy.server.iter_events', return_value=iter(events)):
            status, raw = self.request(body={'input': 'remember', 'stream': True})
        self.assertEqual(status, 200)
        self.assertIn(b'event: response.failed', raw)
        self.assertNotIn(b'event: response.completed', raw)

    def test_compact_replays_full_native_tool_item(self):
        native = {'type': 'function_call', 'id': 'fc_original', 'call_id': 'call_original',
                  'name': 'run_officejs', 'arguments': json.dumps({'summary': 'read a file',
                    'code': json.dumps({'tool': 'read_file', 'args': {'path': 'example.txt'}}),
                    'destructive': False, 'references': ['example.txt']}), 'status': 'completed'}
        self.server.memory.remember(native)
        source = {'input': [{'role': 'user', 'content': 'read a file'},
                            {'type': 'function_call_output', 'call_id': 'call_original', 'output': 'file contents'}]}
        status, _ = self.request(body=source)
        self.assertEqual(status, 200)
        self.assertIn(native, self.sent[-1]['input'])
        self.assertEqual(self.sent[-1]['input'][-1], {'type': 'compaction_trigger'})

    def test_streaming_compact_preserves_native_item(self):
        status, raw = self.request(body={'input': 'remember this', 'stream': True})
        self.assertEqual(status, 200)
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b'data: {')]
        self.assertEqual(events[-1]['type'], 'response.completed')
        self.assertEqual(events[-1]['response']['output'], [OPAQUE])

    def test_compressed_requests_work_on_both_endpoints(self):
        data = json.dumps({'input': 'hello'}).encode()
        for path in ('/v1/responses', '/v1/responses/compact'):
            for coding, raw in [('gzip', gzip.compress(data)), ('deflate', zlib.compress(data)),
                                ('zstd', zstandard.ZstdCompressor().compress(data))]:
                with self.subTest(path=path, coding=coding):
                    status, _ = self.request(path, raw, {'Content-Encoding': coding})
                    self.assertEqual(status, 200)

    def test_bad_compression_and_bombs_fail_before_auth(self):
        for raw, expected in ((b'private-body', 400), (gzip.compress(b'a' * 65536), 413)):
            with patch('bps_proxy.server.MAX_REQUEST_BYTES', 8192), patch('bps_proxy.server.load_session') as auth:
                status, response = self.request(body=raw, headers={'Content-Encoding': 'gzip'})
            self.assertEqual(status, expected)
            self.assertNotIn(b'private-body', response)
            auth.assert_not_called()

    def test_websocket_upgrade_is_explicitly_unsupported(self):
        status, _ = self.request('/v1/responses', method='GET', headers={'Upgrade': 'websocket', 'Connection': 'Upgrade'})
        self.assertEqual(status, 426)
        self.assertFalse(self.sent)

    def test_model_catalog_supports_native_client_shape(self):
        status, raw = self.request('/v1/models?client_version=0.154.0', method='GET')
        self.assertEqual(status, 200)
        result = json.loads(raw)
        self.assertEqual(result['data'], result['models'])
        for model in result['models']:
            self.assertIn('base_instructions', model)
            self.assertIn('truncation_policy', model)
            self.assertEqual(model['experimental_supported_tools'], [])
            self.assertIn('image', model['input_modalities'])

    def test_diagnostics_report_lite_and_inline_compaction_without_secrets(self):
        source = {'input': [{'type': 'additional_tools', 'role': 'developer',
                            'tools': [{'type': 'custom', 'name': 'exec'}]},
                           {'role': 'user', 'content': 'private-prompt-value'},
                           {'type': 'compaction_trigger'}],
                  'prompt_cache_key': 'private-conversation-value'}
        with self.assertLogs('bps_proxy', level='INFO') as captured:
            status, _ = self.request('/v1/responses', source)
            self.request('/private-path-value?secret=private-query-value', method='GET')
        self.assertEqual(status, 200)
        logs = ' '.join(captured.output)
        for expected in ('request_kind=inline_compaction', 'tool_catalog_source=additional_tools',
                         'declared_tools=1', 'queue_wait_ms=', 'content_encoding=identity', 'route=other'):
            self.assertIn(expected, logs)
        for secret in ('private-prompt-value', 'private-conversation-value',
                       'private-path-value', 'private-query-value'):
            self.assertNotIn(secret, logs)


class CompactionWireTest(unittest.TestCase):
    def test_compaction_preserves_instructions_without_transport_prompt(self):
        source = {'input': [{'role': 'user', 'content': 'hi'}],
                  'instructions': 'User supplied instructions', 'parallel_tool_calls': False,
                  'tools': [{'type': 'custom', 'name': 'exec'}]}
        memory = CallMemory()
        memory.bind_tools(source)
        body = prepare_body(compact_request(source), memory)
        self.assertEqual(len(body['input']), 3)
        self.assertEqual(body['input'][0]['content'][0]['text'], source['instructions'])
        self.assertEqual(body['input'][-1], {'type': 'compaction_trigger'})
        self.assertNotIn('run_officejs', json.dumps(body))
        self.assertEqual(memory.bind_tools(source)[0]['name'], 'exec')

    def test_trigger_is_idempotent_and_source_is_unchanged(self):
        source = {'input': [{'role': 'user', 'content': 'hi'}]}
        original = copy.deepcopy(source)
        once = compact_request(source)
        self.assertEqual(compact_request(once), once)
        self.assertEqual(source, original)

    def test_nonterminal_trigger_rejected(self):
        with self.assertRaises(ValueError):
            compact_request({'input': [{'type': 'compaction_trigger'}, {'role': 'user', 'content': 'hi'}]})

    def test_correction_stays_before_trigger(self):
        trigger = {'type': 'compaction_trigger'}
        items = [{'role': 'user', 'content': 'hi'}, trigger]
        extra = [{'role': 'developer', 'content': 'retry'}]
        self.assertEqual(append_input(items, extra), items[:-1] + extra + [trigger])
        self.assertEqual(items[-1], trigger)

    def test_client_context_management_is_preserved(self):
        for policy in ([], [{'type': 'compaction', 'compact_threshold': 12345}]):
            body = prepare_body({'input': [OPAQUE], 'context_management': policy}, CallMemory())
            self.assertEqual(body['context_management'], policy)
            self.assertIn(OPAQUE, body['input'])
