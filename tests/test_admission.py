import threading
import time
import unittest

from bps_proxy.admission import Admission


def wait_for(predicate):
    until = time.monotonic() + 2
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('condition did not become true')


class AdmissionTest(unittest.TestCase):
    def test_fifo_queue_waits_without_exceeding_concurrency(self):
        admission = Admission(2, 3, 2)
        self.assertEqual(admission.acquire().reason, 'accepted')
        self.assertEqual(admission.acquire().reason, 'accepted')
        order, results, threads = [], [], []
        def worker(index):
            result = admission.acquire()
            results.append(result)
            if result.reason == 'accepted':
                order.append(index)
                admission.release()
        for index in range(3):
            thread = threading.Thread(target=worker, args=(index,))
            thread.start()
            threads.append(thread)
            wait_for(lambda: admission.snapshot()[1] == index + 1)
        self.assertEqual(order, [])
        self.assertEqual(admission.acquire().reason, 'full')
        admission.release()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(order, [0, 1, 2])
        self.assertTrue(all(r.reason == 'accepted' and r.active <= 2 for r in results))
        admission.release()
        self.assertEqual(admission.snapshot(), (0, 0))

    def test_timeout_cancel_and_shutdown_remove_waiters(self):
        for reason in ('timeout', 'cancelled', 'closed'):
            with self.subTest(reason=reason):
                admission = Admission(1, 1, 0.1 if reason == 'timeout' else 2)
                admission.acquire()
                cancelled = threading.Event()
                results = []
                thread = threading.Thread(target=lambda: results.append(admission.acquire(cancelled.is_set)))
                thread.start()
                wait_for(lambda: admission.snapshot()[1] == 1)
                if reason == 'cancelled':
                    cancelled.set()
                elif reason == 'closed':
                    admission.close()
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results[0].reason, reason)
                self.assertEqual(admission.snapshot(), (1, 0))
                admission.release()

    def test_invalid_limits_fail_and_double_release_is_detected(self):
        for arguments in ((0, 1, 1), (1, -1, 1), (1, 1, float('nan')), (1, 1, 0)):
            with self.assertRaises(ValueError):
                Admission(*arguments)
        admission = Admission(1, 0, 1)
        admission.acquire()
        self.assertEqual(admission.acquire().reason, 'full')
        admission.release()
        with self.assertRaises(RuntimeError):
            admission.release()
