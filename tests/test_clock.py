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


# -- last completed session (backtest reproducibility) ------------------------
def _clock_with_holidays(settings):
    from kitealgo.holidays import HolidayCalendar
    return SessionClock(settings, HolidayCalendar().add_fixed_holidays(2026))


def test_today_counts_only_after_the_market_closes(settings):
    from datetime import date as _date, datetime
    clock = _clock_with_holidays(settings)

    # Wednesday 2026-09-02, mid-session: today's candles are still forming, so
    # the last completed session is Tuesday the 1st.
    mid_session = datetime(2026, 9, 2, 10, 20, tzinfo=IST)
    assert clock.last_completed_session(mid_session) == _date(2026, 9, 1)

    # Same day at 16:00, after the 15:30 close: today is now complete.
    after_close = datetime(2026, 9, 2, 16, 0, tzinfo=IST)
    assert clock.last_completed_session(after_close) == _date(2026, 9, 2)


def test_weekend_walks_back_to_friday(settings):
    from datetime import datetime
    clock = _clock_with_holidays(settings)
    sunday = datetime(2026, 9, 6, 12, 0, tzinfo=IST)
    assert clock.last_completed_session(sunday).weekday() == 4      # Friday
    assert clock.last_completed_session(sunday) == datetime(2026, 9, 4).date()


def test_before_the_open_uses_the_previous_session(settings):
    from datetime import datetime
    clock = _clock_with_holidays(settings)
    monday_pre_open = datetime(2026, 9, 7, 9, 0, tzinfo=IST)
    assert clock.last_completed_session(monday_pre_open) == datetime(2026, 9, 4).date()


def test_holidays_are_skipped(settings):
    from datetime import datetime
    clock = _clock_with_holidays(settings)
    # 2026-10-02 is Gandhi Jayanti, a Friday — step back to Thursday the 1st.
    after_holiday = datetime(2026, 10, 2, 16, 0, tzinfo=IST)
    assert clock.last_completed_session(after_holiday) == datetime(2026, 10, 1).date()


def test_result_is_always_a_trading_day(settings):
    from datetime import datetime, timedelta
    clock = _clock_with_holidays(settings)
    start = datetime(2026, 9, 1, 12, 0, tzinfo=IST)
    for offset in range(45):
        when = start + timedelta(days=offset)
        session = clock.last_completed_session(when)
        assert clock.holidays.is_trading_day(session)
        assert session <= when.date()
