import time

import pytest

from kitealgo.ratelimit import RateLimiter


def test_allows_burst_up_to_the_limit():
    limiter = RateLimiter(max_calls=5, period=1.0)
    started = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    assert time.monotonic() - started < 0.05      # no blocking within the budget


def test_blocks_once_the_budget_is_spent():
    limiter = RateLimiter(max_calls=2, period=0.3)
    started = time.monotonic()
    for _ in range(3):
        limiter.acquire()
    assert time.monotonic() - started >= 0.25     # the third call had to wait


def test_rejects_nonsense_configuration():
    with pytest.raises(ValueError):
        RateLimiter(max_calls=0)


def test_works_as_a_context_manager():
    limiter = RateLimiter(max_calls=1, period=0.01)
    with limiter:
        pass
