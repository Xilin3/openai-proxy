import importlib.util
import io
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from bps_proxy.auth import ChatGPTSession
from bps_proxy.upstream import UpstreamError, iter_events


class StreamResourceTests(unittest.TestCase):
    session = ChatGPTSession('fake', 'account', '', 0)

    def test_idle_stream_closes_blocked_reader(self):
        closed = threading.Event()
        class Response:
            headers = {'Content-Type': 'text/event-stream'}
            def readline(self, _size=-1):
                closed.wait(2)
                return b''
            def close(self):
                closed.set()
        before = {thread.ident for thread in threading.enumerate() if thread.name == 'bps-upstream'}
        started = time.monotonic()
        with patch('bps_proxy.upstream.request.urlopen', return_value=Response()), patch('bps_proxy.upstream.UPSTREAM_IDLE_TIMEOUT', 0.03):
            with self.assertRaises(UpstreamError) as raised:
                list(iter_events(self.session, {}))
        self.assertEqual(raised.exception.status, 504)
        self.assertTrue(closed.is_set())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual({thread.ident for thread in threading.enumerate() if thread.name == 'bps-upstream'}, before)

    def test_oversized_single_line_fails_without_unbounded_read(self):
        class Response:
            headers = {'Content-Type': 'text/event-stream'}
            closed = False
            def readline(self, size=-1):
                self.requested = size
                return b'x' * size
            def close(self):
                self.closed = True
        response = Response()
        with patch('bps_proxy.upstream.request.urlopen', return_value=response), patch('bps_proxy.upstream.MAX_EVENT_BYTES', 128):
            with self.assertRaises(UpstreamError):
                list(iter_events(self.session, {}))
        self.assertEqual(response.requested, 129)
        self.assertTrue(response.closed)

    def test_generator_close_unblocks_full_queue_producer(self):
        response = io.BytesIO(b'data: {"type":"response.created"}\n\n' + b': keepalive\n' * 10000)
        response.headers = {'Content-Type': 'text/event-stream'}
        with patch('bps_proxy.upstream.request.urlopen', return_value=response), patch('bps_proxy.upstream.MAX_QUEUED_LINES', 1):
            events = iter_events(self.session, {})
            next(events)
            events.close()
        self.assertTrue(response.closed)


class CandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'tools' / 'candidate.py'
        spec = importlib.util.spec_from_file_location('candidate_test_module', path)
        cls.candidate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.candidate)

    def test_live_port_is_never_started_or_stopped_by_candidate_start(self):
        with patch.object(self.candidate, 'record', return_value={}), patch.object(self.candidate.subprocess, 'Popen') as popen:
            with self.assertRaises(RuntimeError):
                self.candidate.start(8787)
        popen.assert_not_called()

    def test_cli_provider_override_is_ephemeral(self):
        args = self.candidate.cli_args(18787, ['exec', 'hello'])
        self.assertEqual(args[0], 'codex')
        self.assertEqual(args[-2:], ['exec', 'hello'])
        self.assertIn('model_providers.bps.base_url="http://127.0.0.1:18787/v1"', args)
        self.assertFalse(any('config.toml' in arg for arg in args))

    def test_stop_does_not_signal_an_unowned_pid(self):
        with patch.object(self.candidate, 'record', return_value={'pid': 1, 'root': '/another/root'}), patch.object(self.candidate.os, 'kill') as kill:
            self.candidate.stop()
        kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
