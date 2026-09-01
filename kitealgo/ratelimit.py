"""A tiny thread-safe rate limiter.

Kite enforces per-endpoint request ceilings (roughly 10/s for orders and
quotes, 3/s for historical data).  Blowing through them gets you HTTP 429s and,
repeatedly, a suspended app — so every network call in this package goes
through one of these.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Allow at most `max_calls` in any `period` seconds, blocking when needed."""

    def __init__(self, max_calls: int, period: float = 1.0) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be >= 1")
        self.max_calls = max_calls
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                sleep_for = self.period - (now - self._calls[0])
            time.sleep(max(sleep_for, 0.001))

    def __enter__(self) -> "RateLimiter":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        return None


# Shared limiters, sized just under Kite's documented ceilings.
ORDER_LIMITER = RateLimiter(max_calls=8, period=1.0)
QUOTE_LIMITER = RateLimiter(max_calls=8, period=1.0)
HISTORICAL_LIMITER = RateLimiter(max_calls=2, period=1.0)
