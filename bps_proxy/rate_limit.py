# Bound outgoing request starts without holding a lock during network I/O.
from __future__ import annotations

from collections import deque
import math
import threading
import time

UPSTREAM_REQUESTS_PER_SECOND = 5


class RequestRateLimiter:
    def __init__(self, limit=UPSTREAM_REQUESTS_PER_SECOND, window=1.0, *, clock=None):
        if type(limit) is not int or limit < 1:
            raise ValueError('每秒上游请求数必须是正整数')
        if not isinstance(window, (int, float)) or not math.isfinite(window) or window <= 0:
            raise ValueError('请求限速窗口必须是有限正数')
        self.limit, self.window = limit, window
        self._clock = clock or time.monotonic
        self._condition = threading.Condition()
        self._starts = deque()
        self._waiters = deque()
        self._closed = False

    def acquire(self, cancelled=None):
        # Admitted handlers bound this queue. FIFO prevents retry starvation.
        ticket = object()
        with self._condition:
            self._waiters.append(ticket)
            try:
                while True:
                    if self._closed or (cancelled is not None and cancelled()):
                        return False
                    now = self._clock()
                    while self._starts and self._starts[0] <= now - self.window:
                        self._starts.popleft()
                    if self._waiters[0] is ticket and len(self._starts) < self.limit:
                        self._starts.append(now)
                        return True
                    delay = self._starts[0] + self.window - now if self._starts else 0.1
                    self._condition.wait(min(max(delay, 0.001), 0.1))
            finally:
                self._waiters.remove(ticket)
                self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
