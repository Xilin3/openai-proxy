"""Bounded FIFO admission without holding request bodies in a waiting queue."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import threading
import time


@dataclass(frozen=True)
class AdmissionResult:
    reason: str
    wait_ms: int
    active: int
    queued: int


class Admission:
    def __init__(self, limit=8, max_pending=32, timeout=120.0):
        if type(limit) is not int or limit < 1:
            raise ValueError('并发数必须是正整数')
        if type(max_pending) is not int or max_pending < 0:
            raise ValueError('等待队列容量必须是非负整数')
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('排队超时必须是有限正数')
        self.limit, self.max_pending, self.timeout = limit, max_pending, timeout
        self._condition = threading.Condition()
        self._active = 0
        self._waiters = deque()
        self._closed = False

    def snapshot(self):
        with self._condition:
            return self._active, len(self._waiters)

    def acquire(self, cancelled=None):
        started = time.monotonic()
        ticket = object()
        with self._condition:
            def result(reason):
                return AdmissionResult(reason, int((time.monotonic() - started) * 1000),
                                       self._active, len(self._waiters) - int(ticket in self._waiters))
            if self._closed:
                return result('closed')
            if cancelled is not None and cancelled():
                return result('cancelled')
            if self._active < self.limit and not self._waiters:
                self._active += 1
                return result('accepted')
            if len(self._waiters) >= self.max_pending:
                return result('full')
            self._waiters.append(ticket)
            try:
                while True:
                    if self._closed:
                        return result('closed')
                    if cancelled is not None and cancelled():
                        return result('cancelled')
                    if self._waiters[0] is ticket and self._active < self.limit:
                        self._active += 1
                        return result('accepted')
                    remaining = self.timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        return result('timeout')
                    self._condition.wait(min(remaining, 0.1))
            finally:
                self._waiters.remove(ticket)
                self._condition.notify_all()

    def release(self):
        with self._condition:
            if self._active < 1:
                raise RuntimeError('admission release without acquisition')
            self._active -= 1
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
