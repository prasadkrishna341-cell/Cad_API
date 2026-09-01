from datetime import datetime

import pytest

from kitealgo.clock import SessionClock, is_market_open, now_ist
from kitealgo.config import IST

TUESDAY = datetime(2026, 9, 1, tzinfo=IST)
SATURDAY = datetime(2026, 9, 5, tzinfo=IST)


@pytest.fixture
def clock(settings):
    return SessionClock(settings)


def at(hour, minute, day=TUESDAY):
    return day.replace(hour=hour, minute=minute)


def test_entries_only_inside_the_configured_window(clock):
    assert not clock.can_enter(at(9, 10))     # before trade_start
    assert clock.can_enter(at(9, 20))         # at trade_start
    assert clock.can_enter(at(14, 59))
    assert not clock.can_enter(at(15, 0))     # at trade_end, exclusive


def test_no_entries_at_the_weekend(clock):
    assert not clock.can_enter(at(10, 0, SATURDAY))


def test_square_off_window(clock):
    assert not clock.should_square_off(at(15, 14))
    assert clock.should_square_off(at(15, 15))
    assert clock.should_square_off(at(15, 29))


def test_session_over_at_market_close(clock):
    assert not clock.is_session_over(at(15, 29))
    assert clock.is_session_over(at(15, 30))


def test_market_open_hours():
    assert not is_market_open(at(9, 14))
    assert is_market_open(at(9, 15))
    assert is_market_open(at(15, 30))
    assert not is_market_open(at(15, 31))
    assert not is_market_open(at(10, 0, SATURDAY))


def test_naive_datetimes_are_treated_as_ist(clock):
    assert clock.can_enter(datetime(2026, 9, 1, 10, 0))


def test_describe_is_human_readable(clock):
    assert "weekend" in clock.describe(at(10, 0, SATURDAY))
    assert "pre-open" in clock.describe(at(9, 0))
    assert "entries allowed" in clock.describe(at(10, 0))
    assert "square-off" in clock.describe(at(15, 20))


def test_now_ist_is_timezone_aware():
    assert now_ist().tzinfo is not None
