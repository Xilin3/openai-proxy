import http.client
import json
import os
import socket
import struct
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from bps_proxy.server import ProxyServer
from bps_proxy.rate_limit import RequestRateLimiter
from bps_proxy.auth import AuthError
from bps_proxy.wire import CallMemory
from tests.test_admission import wait_for
from tests.test_compatibility import completion


class AdmissionHttpTest(unittest.TestCase):
    def test_auth_and_relay_errors_release_active_capacity(self):
        server = ProxyServer(('127.0.0.1', 0), CallMemory(), max_concurrent=1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for failure, expected in (('auth', 401), ('relay', 502)):
                with self.subTest(failure=failure), patch('bps_proxy.server.load_session',
                        side_effect=AuthError('test failure') if failure == 'auth' else None, return_value=None), patch(
                        'bps_proxy.server.iter_events', side_effect=RuntimeError('test failure')):
                    conn = http.client.HTTPConnection(*server.server_address, timeout=3)
                    try:
                        conn.request('POST', '/v1/responses', json.dumps({'input': 'hi'}))
                        response = conn.getresponse()
                        response.read()
                        self.assertEqual(response.status, expected)
                    finally:
                        conn.close()
                    wait_for(lambda: server.admission.snapshot() == (0, 0))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    @unittest.skipIf(os.name == 'nt', 'POSIX SO_LINGER fixture')
    def test_reset_removes_waiter_but_half_close_is_valid(self):
        server = ProxyServer(('127.0.0.1', 0), CallMemory(), max_concurrent=1, max_pending=1, queue_timeout=2)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        server.admission.acquire()
        body = json.dumps({'input': 'hi'}).encode()
        raw = ('POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1:' + str(server.server_port)
               + '\r\nContent-Length: ' + str(len(body)) + '\r\n\r\n').encode() + body
        try:
            with patch('bps_proxy.server.load_session', return_value=None), patch(
                    'bps_proxy.server.iter_events', side_effect=lambda *args: iter([completion([])])) as upstream:
                conn = socket.create_connection(server.server_address, timeout=3)
                conn.sendall(raw)
                wait_for(lambda: server.admission.snapshot() == (1, 1))
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                conn.close()
                wait_for(lambda: server.admission.snapshot() == (1, 0))
                upstream.assert_not_called()
                with socket.create_connection(server.server_address, timeout=3) as conn:
                    conn.sendall(raw)
                    conn.shutdown(socket.SHUT_WR)
                    wait_for(lambda: server.admission.snapshot() == (1, 1))
                    server.admission.release()
                    response = http.client.HTTPResponse(conn)
                    response.begin()
                    response.read()
                    self.assertEqual(response.status, 200)
                wait_for(lambda: server.admission.snapshot() == (0, 0))
                upstream.assert_called_once()
        finally:
            if server.admission.snapshot()[0]:
                server.admission.release()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def run_scenario(self, timeout):
        server = ProxyServer(('127.0.0.1', 0), CallMemory(),
                             max_concurrent=1, max_pending=1, queue_timeout=timeout)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        release = threading.Event()
        entered = threading.Event()
        calls = []
        def upstream(session, body):
            calls.append(body)
            entered.set()
            release.wait(3)
            return iter([completion([])])
        def request():
            conn = http.client.HTTPConnection(*server.server_address, timeout=4)
            try:
                conn.request('POST', '/v1/responses', json.dumps({'input': 'hi'}))
                response = conn.getresponse()
                response.read()
                return response.status, response.getheader('Retry-After')
            finally:
                conn.close()
        thread.start()
        try:
            with patch('bps_proxy.server.load_session', return_value=None), patch(
                    'bps_proxy.server.iter_events', side_effect=upstream), ThreadPoolExecutor(3) as pool:
                try:
                    first = pool.submit(request)
                    self.assertTrue(entered.wait(2))
                    second = pool.submit(request)
                    wait_for(lambda: server.admission.snapshot() == (1, 1))
                    self.assertEqual(request(), (503, '1'))
                    if timeout < 1:
                        self.assertEqual(second.result(2), (503, '1'))
                        wait_for(lambda: server.admission.snapshot() == (1, 0))
                    else:
                        self.assertFalse(second.done())
                    release.set()
                    self.assertEqual(first.result(2), (200, None))
                    if timeout >= 1:
                        self.assertEqual(second.result(2), (200, None))
                    wait_for(lambda: server.admission.snapshot() == (0, 0))
                    self.assertEqual(len(calls), 1 if timeout < 1 else 2)
                finally:
                    release.set()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_overflow_waits_and_queue_full_has_retry_after(self):
        self.run_scenario(2)

    def test_wait_timeout_does_not_leak_capacity(self):
        self.run_scenario(0.15)

    def test_default_eight_requests_overlap_and_ninth_waits(self):
        server = ProxyServer(('127.0.0.1', 0), CallMemory())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        release, lock = threading.Event(), threading.Lock()
        starts = []
        def upstream(session, body):
            with lock:
                starts.append(time.monotonic())
            if not release.wait(5):
                raise RuntimeError('fixture did not release upstream')
            return iter([completion([])])
        def request(index):
            conn = http.client.HTTPConnection(*server.server_address, timeout=6)
            try:
                model = 'gpt-6-astra' if index % 2 else 'gpt-5.6-sol'
                conn.request('POST', '/v1/responses', json.dumps({
                    'input': 'hi', 'model': model, 'prompt_cache_key': str(index)}))
                response = conn.getresponse()
                response.read()
                return response.status
            finally:
                conn.close()
        thread.start()
        try:
            with patch('bps_proxy.server.load_session', return_value=None), patch(
                    'bps_proxy.server.iter_events', side_effect=upstream), ThreadPoolExecutor(9) as pool:
                try:
                    futures = [pool.submit(request, index) for index in range(8)]
                    wait_for(lambda: len(starts) == 8)
                    self.assertEqual(server.admission.snapshot(), (8, 0))
                    self.assertGreaterEqual(starts[5] - starts[0], 0.95)
                    ninth = pool.submit(request, 8)
                    wait_for(lambda: server.admission.snapshot() == (8, 1))
                    self.assertFalse(ninth.done())
                    self.assertEqual(len(starts), 8)
                    health = http.client.HTTPConnection(*server.server_address, timeout=2)
                    try:
                        health.request('GET', '/health')
                        response = health.getresponse()
                        response.read()
                        self.assertEqual(response.status, 200)
                    finally:
                        health.close()
                    release.set()
                    self.assertEqual([future.result(3) for future in futures + [ninth]], [200] * 9)
                    wait_for(lambda: server.admission.snapshot() == (0, 0))
                    self.assertEqual(len(starts), 9)
                finally:
                    release.set()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    @unittest.skipIf(os.name == 'nt', 'POSIX SO_LINGER fixture')
    def test_reset_during_rate_wait_does_not_dispatch_or_leak_admission(self):
        server = ProxyServer(('127.0.0.1', 0), CallMemory())
        server.upstream_rate = RequestRateLimiter(1, window=60)
        server.upstream_rate.acquire()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        body = json.dumps({'input': 'hi'}).encode()
        lines = ['POST /v1/responses HTTP/1.1', 'Host: 127.0.0.1:' + str(server.server_port),
                 'Content-Length: ' + str(len(body)), '', '']
        raw = (chr(13) + chr(10)).join(lines).encode() + body
        try:
            with patch('bps_proxy.server.load_session', return_value=None), patch(
                    'bps_proxy.server.iter_events') as upstream:
                with socket.create_connection(server.server_address, timeout=3) as conn:
                    conn.sendall(raw)
                    wait_for(lambda: len(server.upstream_rate._waiters) == 1)
                    self.assertEqual(server.admission.snapshot(), (1, 0))
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                wait_for(lambda: server.admission.snapshot() == (0, 0))
                self.assertEqual(len(server.upstream_rate._waiters), 0)
                upstream.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
