import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bps_proxy.images import Pictures
from bps_proxy.rate_limit import RequestRateLimiter
from bps_proxy.server import Handler
from bps_proxy.upstream import UpstreamError
from bps_proxy.wire import CallMemory
from tests.fixtures import picture
from tests.test_admission import wait_for
from tests.test_forward import completed, transport, TOOLS


class RateLimitTest(unittest.TestCase):
    def test_five_starts_share_a_rolling_second(self):
        now = [10.0]
        limiter = RequestRateLimiter(clock=lambda: now[0])
        self.addCleanup(limiter.close)
        for _ in range(5):
            self.assertTrue(limiter.acquire())
        result = []
        done = threading.Event()
        def worker():
            result.append(limiter.acquire())
            done.set()
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(limiter.close)
        wait_for(lambda: len(limiter._waiters) == 1)
        with limiter._condition:
            now[0] = 10.999
            limiter._condition.notify_all()
        self.assertFalse(done.wait(0.03))
        with limiter._condition:
            now[0] = 11.0
            limiter._condition.notify_all()
        self.assertTrue(done.wait(2))
        self.assertEqual(result, [True])
        self.assertEqual(list(limiter._starts), [11.0])

    def test_cancelled_head_does_not_consume_the_next_window(self):
        now = [10.0]
        limiter = RequestRateLimiter(1, clock=lambda: now[0])
        limiter.acquire()
        cancelled = threading.Event()
        results, threads = {}, []
        def worker(index, cancellation):
            results[index] = limiter.acquire(cancellation)
        try:
            for index, cancellation in enumerate((cancelled.is_set, None)):
                thread = threading.Thread(target=worker, args=(index, cancellation), daemon=True)
                threads.append(thread)
                thread.start()
                wait_for(lambda: len(limiter._waiters) == index + 1)
            cancelled.set()
            threads[0].join(2)
            self.assertFalse(threads[0].is_alive())
            self.assertFalse(results[0])
            self.assertEqual(list(limiter._starts), [10.0])
            with limiter._condition:
                now[0] = 11.0
                limiter._condition.notify_all()
            threads[1].join(2)
            self.assertFalse(threads[1].is_alive())
            self.assertTrue(results[1])
        finally:
            limiter.close()
            for thread in threads:
                thread.join(2)

    def test_close_wakes_waiter_and_refuses_new_starts(self):
        limiter = RequestRateLimiter(1, window=60)
        limiter.acquire()
        result = []
        thread = threading.Thread(target=lambda: result.append(limiter.acquire()), daemon=True)
        thread.start()
        try:
            wait_for(lambda: len(limiter._waiters) == 1)
        finally:
            limiter.close()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        self.assertFalse(limiter.acquire())
        self.assertEqual(len(limiter._waiters), 0)

    def test_invalid_limits_fail(self):
        for kwargs in ({'limit': 0}, {'limit': -1}, {'limit': True}, {'limit': 1.5},
                       {'window': 0}, {'window': float('nan')}, {'window': float('inf')}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RequestRateLimiter(**kwargs)


class RateLimitRelayTest(unittest.TestCase):
    def handler(self):
        handler = Handler.__new__(Handler)
        handler.connection = Mock()
        handler.connection.getsockopt.return_value = 0
        handler.server = SimpleNamespace(memory=CallMemory(), pictures=Pictures(),
                                         upstream_rate=Mock())
        handler.server.upstream_rate.acquire.return_value = True
        return handler

    def test_image_retry_and_actual_upload_share_the_request_budget(self):
        handler = self.handler()
        session = SimpleNamespace(account_id='rate-test')
        body = {'input': [{'role': 'user', 'content': [picture()]}]}
        order = []
        handler.server.upstream_rate.acquire.side_effect = lambda *_: order.append('admit') or True
        attempts = []
        def upstream(_session, request, **_kwargs):
            order.append('responses')
            attempts.append(request)
            if len(attempts) == 1:
                raise UpstreamError(422, 'inline image rejected')
            return iter([completed([])])
        def upload(*_):
            order.append('upload')
            return 'file_rate_test'
        with patch('bps_proxy.server.iter_events', side_effect=upstream), patch(
                'bps_proxy.images.upload', side_effect=upload):
            self.assertEqual(list(handler._iter_relay(body, session))[-1][0], 'response.completed')
            self.assertEqual(order, ['admit', 'responses', 'admit', 'upload', 'admit', 'responses'])
            order.clear()
            list(handler._iter_relay(body, session))
            self.assertEqual(order, ['admit', 'responses'])

    def test_failed_attempt_keeps_its_budget_and_next_request_is_counted(self):
        handler = self.handler()
        with patch('bps_proxy.server.iter_events', side_effect=[
                UpstreamError(502, 'failed'), iter([completed([])])]) as upstream:
            with self.assertRaises(UpstreamError):
                list(handler._iter_relay({'input': 'first'}, None))
            list(handler._iter_relay({'input': 'second'}, None))
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(handler.server.upstream_rate.acquire.call_count, 2)

    def test_tool_format_correction_counts_as_another_upstream_attempt(self):
        handler = self.handler()
        batches = [iter([completed([transport('call_bad', '{broken')])]),
                   iter([completed([])])]
        with patch('bps_proxy.server.iter_events', side_effect=batches) as upstream:
            events = list(handler._iter_relay({'input': 'hi', 'tools': TOOLS}, None))
        self.assertEqual(events[-1][0], 'response.completed')
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(handler.server.upstream_rate.acquire.call_count, 2)

    def test_cancelled_pacing_never_opens_upstream(self):
        handler = self.handler()
        handler.server.upstream_rate.acquire.return_value = False
        with patch('bps_proxy.server.iter_events') as upstream:
            with self.assertRaises(ConnectionResetError):
                list(handler._iter_relay({'input': 'cancel'}, None))
        upstream.assert_not_called()
