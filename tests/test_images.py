import base64
import io
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from bps_proxy.images import Pictures, ImageInputError, UploadError, upload
from bps_proxy.server import Handler
from bps_proxy.upstream import UpstreamError, attachment_url
from bps_proxy.wire import CallMemory, StreamRewriter, prepare_body, translate_input
from tests.fixtures import png, picture
from tests.test_forward import completed, transport, TOOLS


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(account_id='account-a', account_user_id='', access_token='fake')
        self.body = {'input': [{'role': 'user', 'content': [picture()]}]}
        self.images = Pictures()

    def test_attachment_upload_is_multipart_and_uses_openai_file_id(self):
        response = io.BytesIO(b'{"openai_file_id":"file_test"}')
        with patch('bps_proxy.images.request.urlopen', return_value=response) as opened:
            self.assertEqual(upload(self.session, 'image/png', png(), 'ab' * 32), 'file_test')
        req = opened.call_args.args[0]
        self.assertTrue(attachment_url().endswith('/attachments'))
        self.assertEqual(req.full_url, attachment_url())
        self.assertIn(b'name="file"', req.data)
        self.assertIn(png(), req.data)
        self.assertTrue(req.get_header('Content-type').startswith('multipart/form-data; boundary='))
        self.assertTrue(response.closed)

    def test_invalid_images_fail_before_any_upload(self):
        cases = [
            'data:image/png;base64,%%%bad',
            'data:image/svg+xml;base64,' + base64.b64encode(b'<svg/>').decode(),
            'data:image/jpeg;base64,' + base64.b64encode(png()).decode(),
            'data:image/png;base64,' + base64.b64encode(b'png-bytes').decode(),
        ]
        self.images.refuse_inline('account-a', {'message'})
        for url in cases:
            body = {'input': [{'role': 'user', 'content': [picture(), {'type': 'input_image', 'image_url': url}]}]}
            with self.subTest(url=url[:30]), patch('bps_proxy.images.upload') as uploaded:
                with self.assertRaises(ImageInputError):
                    self.images.rewrite(body, self.session)
                uploaded.assert_not_called()

    def test_byte_and_pixel_limits(self):
        with patch('bps_proxy.images.MAX_IMAGE_BYTES', 1), self.assertRaises(ImageInputError):
            self.images.rewrite(self.body, self.session)
        with patch('bps_proxy.images.MAX_REQUEST_IMAGE_BYTES', 1), self.assertRaises(ImageInputError):
            self.images.rewrite(self.body, self.session)
        with patch('bps_proxy.images.MAX_PIXELS', 1), self.assertRaises(ImageInputError):
            self.images.rewrite(self.body, self.session)

    def test_more_than_twenty_images_reach_upstream_intact(self):
        image_sets = {
            'inline': [picture((index, 0, 0)) for index in range(21)],
            'attachments': [{'type': 'input_image', 'file_id': f'file_{index}'} for index in range(21)],
        }
        for image_form, images in image_sets.items():
            for output_type in ('function_call_output', 'custom_tool_call_output'):
                with self.subTest(image_form=image_form, output_type=output_type):
                    body = {'input': [
                        {'role': 'user', 'content': images[:10]},
                        {'type': output_type, 'call_id': 'call_history', 'output': images[10:20]},
                        {'role': 'user', 'content': images[20:]},
                    ]}
                    original = json.dumps(body)
                    handler = Handler.__new__(Handler)
                    handler.server = SimpleNamespace(memory=CallMemory(), pictures=Pictures())
                    with patch('bps_proxy.server.iter_events', return_value=iter([completed([])])) as upstream, patch('bps_proxy.images.upload') as uploaded:
                        events = list(handler._iter_relay(body, self.session))
                    upstream.assert_called_once()
                    uploaded.assert_not_called()
                    forwarded = []
                    for item in upstream.call_args.args[1]['input']:
                        field = 'output' if item.get('type') in ('function_call_output', 'custom_tool_call_output') else 'content'
                        forwarded.extend(part for part in item.get(field, [])
                                         if isinstance(part, dict) and part.get('type') == 'input_image')
                    self.assertEqual(forwarded, images)
                    self.assertEqual(events[-1][0], 'response.completed')
                    self.assertEqual(json.dumps(body), original)

    def test_repeated_images_are_preserved_beyond_twenty(self):
        body = {'input': [{'role': 'user', 'content': [picture() for _ in range(21)]}]}
        self.assertEqual(self.images.rewrite(body, self.session).body, body)

    def test_large_image_history_keeps_byte_limit_before_upload(self):
        body = {'input': [{'role': 'user', 'content': [picture() for _ in range(21)]}]}
        self.images.refuse_inline('account-a', {'message'})
        with patch('bps_proxy.images.MAX_REQUEST_IMAGE_BYTES', len(png()) * 20), patch('bps_proxy.images.upload') as uploaded:
            with self.assertRaisesRegex(ImageInputError, '图片合计超过'):
                self.images.rewrite(body, self.session)
        uploaded.assert_not_called()

    def test_upload_failure_never_replaces_picture_with_text(self):
        original = json.dumps(self.body)
        self.images.refuse_inline('account-a', {'message'})
        with patch('bps_proxy.images.upload', side_effect=UploadError('upload failed')):
            with self.assertRaises(UploadError):
                self.images.rewrite(self.body, self.session)
        self.assertEqual(json.dumps(self.body), original)

    def test_upload_error_does_not_echo_server_body(self):
        response = io.BytesIO(b'private-token-and-debug-data')
        error = HTTPError(attachment_url(), 403, 'private', {}, response)
        with patch('bps_proxy.images.request.urlopen', side_effect=error), self.assertRaises(UploadError) as raised:
            upload(self.session, 'image/png', png(), 'ab')
        self.assertEqual(raised.exception.status, 403)
        self.assertNotIn('private', str(raised.exception))
        self.assertTrue(response.closed)

    def test_same_picture_concurrently_uploads_once(self):
        self.images.refuse_inline('account-a', {'message'})
        def slow(*_):
            time.sleep(0.02)
            return 'file_one'
        with patch('bps_proxy.images.upload', side_effect=slow) as uploaded, ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.images.rewrite(self.body, self.session).body, range(16)))
        self.assertEqual(uploaded.call_count, 1)
        self.assertTrue(all(item['input'][0]['content'][0]['file_id'] == 'file_one' for item in results))

    def test_accounts_do_not_share_refusal_or_attachment_cache(self):
        other = SimpleNamespace(account_id='account-b')
        self.images.refuse_inline('account-a', {'message'})
        with patch('bps_proxy.images.upload', side_effect=['file_a', 'file_b']) as uploaded:
            first = self.images.rewrite(self.body, self.session)
            self.assertEqual(self.images.rewrite(self.body, other).inline, {'message'})
            self.images.refuse_inline('account-b', {'message'})
            second = self.images.rewrite(self.body, other)
            self.images.forget('account-a', {'file_b'})
            third = self.images.rewrite(self.body, other)
        self.assertEqual(uploaded.call_count, 2)
        self.assertEqual(first.file_ids, {'file_a'})
        self.assertEqual(second.file_ids, third.file_ids)

    def test_tool_screenshot_array_survives_translation(self):
        for kind in ('function_call_output', 'custom_tool_call_output'):
            parts = [{'type': 'input_text', 'text': 'screenshot'}, picture()]
            result = translate_input([{'type': kind, 'call_id': 'call_1', 'output': parts}], CallMemory())
            self.assertEqual(result[-1]['output'], parts)
            self.images.refuse_inline('account-a', {kind})
            body = {'input': [{'type': kind, 'call_id': 'call_1', 'output': parts}]}
            with patch('bps_proxy.images.upload', return_value='file_shot'):
                sent = self.images.rewrite(body, self.session)
            replay = translate_input(sent.body['input'], CallMemory())
            self.assertEqual(replay[-1]['output'][1]['file_id'], 'file_shot')
            self.assertEqual(replay[-1]['output'][0]['text'], 'screenshot')

    def test_rejected_inline_uploads_then_retries_same_turn(self):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(memory=CallMemory(), pictures=self.images)
        requests = []
        def upstream(_session, body, **_kwargs):
            requests.append(body)
            if len(requests) == 1:
                raise UpstreamError(422, 'invalid body')
            return iter([completed([])])
        with patch('bps_proxy.server.iter_events', side_effect=upstream), patch('bps_proxy.images.upload', return_value='file_ok') as uploaded:
            events = list(handler._iter_relay(self.body, self.session))
        self.assertEqual(events[-1][0], 'response.completed')
        self.assertEqual(uploaded.call_count, 1)
        self.assertEqual(requests[0]['metadata']['turn_id'], requests[1]['metadata']['turn_id'])
        self.assertEqual([item['metadata']['agent_iteration'] for item in requests], ['1', '2'])
        self.assertIn('file_ok', json.dumps(requests[1]))
        self.assertNotIn('data:image', json.dumps(requests[1]))

    def test_expired_cached_attachment_is_reuploaded_once(self):
        self.images.refuse_inline('account-a', {'message'})
        with patch('bps_proxy.images.upload', return_value='file_old'):
            self.images.rewrite(self.body, self.session)
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(memory=CallMemory(), pictures=self.images)
        with patch('bps_proxy.server.iter_events', side_effect=[UpstreamError(400, 'expired'), iter([completed([])])]) as upstream, patch('bps_proxy.images.upload', return_value='file_new') as uploaded:
            events = list(handler._iter_relay(self.body, self.session))
        self.assertEqual(uploaded.call_count, 1)
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(events[-1][0], 'response.completed')

    def test_fresh_attachment_rejection_never_omits_picture(self):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(memory=CallMemory(), pictures=self.images)
        with patch('bps_proxy.server.iter_events', side_effect=UpstreamError(422, 'invalid')) as upstream, patch('bps_proxy.images.upload', return_value='file_new') as uploaded:
            with self.assertRaises(UpstreamError):
                list(handler._iter_relay(self.body, self.session))
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(uploaded.call_count, 1)

    def test_image_cache_evicts_old_entries(self):
        self.images.refuse_inline('account-a', {'message'})
        with patch('bps_proxy.images.CACHE_SIZE', 1), patch('bps_proxy.images.upload', side_effect=['a', 'b', 'c']) as uploaded:
            self.images.rewrite(self.body, self.session)
            self.images.rewrite({'input': [{'role': 'user', 'content': [picture((0, 255, 0))]}]}, self.session)
            self.images.rewrite(self.body, self.session)
        self.assertEqual(uploaded.call_count, 3)


class TerminalTests(unittest.TestCase):
    def test_text_delta_is_delivered_before_upstream_continues(self):
        consumed = []
        def upstream(*_, **_kwargs):
            consumed.append('start')
            yield 'response.output_text.delta', {'type': 'response.output_text.delta', 'output_index': 0, 'delta': 'hello'}
            consumed.append('finish')
            yield completed([])
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(memory=CallMemory())
        with patch('bps_proxy.server.iter_events', side_effect=upstream):
            relay = handler._iter_relay({'input': 'hi'}, None)
            self.assertEqual(next(relay)[0], 'response.output_text.delta')
            self.assertEqual(consumed, ['start'])
            relay.close()

    def test_failed_or_missing_terminal_never_releases_tools(self):
        native = transport('call_1', json.dumps({'tool': 'exec', 'input': 'dangerous command'}))
        for terminal in ([], [('response.failed', {'response': {'status': 'failed', 'output': [native]}})]):
            memory = CallMemory()
            handler = Handler.__new__(Handler)
            handler.server = SimpleNamespace(memory=memory)
            events = [('response.output_item.done', {'output_index': 0, 'item': native})] + terminal
            with patch('bps_proxy.server.iter_events', return_value=iter(events)):
                output = list(handler._iter_relay({'input': 'hi', 'tools': TOOLS}, None))
            self.assertEqual(output[-1][0], 'response.failed')
            self.assertFalse(any(name == 'response.output_item.added' for name, _ in output))
            self.assertIsNone(memory.recall('call_1'))

    def test_tool_choice_none_cannot_enable_implicit_exec(self):
        memory = CallMemory()
        memory.bind_tools({'input': 'hi', 'tools': TOOLS})
        native = transport('call_1', json.dumps({'tool': 'exec', 'input': 'pwd'}))
        rewriter = StreamRewriter(memory.bind_tools({'input': 'hi', 'tool_choice': 'none'}), memory)
        events = rewriter.handle(*completed([native]))
        self.assertEqual(events[-1][1]['response']['output'], [])
        self.assertIsNone(memory.recall('call_1'))

    def test_scoped_replay_cannot_cross_accounts_or_threads(self):
        memory = CallMemory()
        a, b, c = [memory.scoped(scope) for scope in ('account-a:thread-a', 'account-b:thread-a', 'account-a:thread-b')]
        native = transport('same_id', json.dumps({'tool': 'exec', 'input': 'pwd'}))
        a.remember(native)
        self.assertEqual(a.recall('same_id'), native)
        self.assertIsNone(b.recall('same_id'))
        self.assertIsNone(c.recall('same_id'))

    def test_parallel_results_count_as_one_round(self):
        body = {'input': [{'role': 'user', 'content': 'hi'},
                          {'type': 'function_call_output', 'call_id': 'a', 'output': 'a'},
                          {'type': 'function_call_output', 'call_id': 'b', 'output': 'b'}]}
        self.assertEqual(prepare_body(body, CallMemory())['metadata']['agent_iteration'], '2')

    def test_cache_permissions_and_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'calls.json'
            memory = CallMemory(path)
            item = transport('call_1', json.dumps({'tool': 'exec', 'input': 'pwd'}))
            memory.remember(item)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch('bps_proxy.wire.os.replace', side_effect=OSError('disk full')), self.assertLogs('bps_proxy', 'WARNING'):
                memory.advance_turn('turn', 4)
            self.assertEqual(memory.advance_turn('turn', 1), 4)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
