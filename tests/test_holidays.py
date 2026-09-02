from datetime import date, datetime, timedelta

import pytest

from kitealgo.clock import SessionClock
from kitealgo.config import IST
from kitealgo.holidays import HolidayCalendar, fixed_holidays_for

REPUBLIC_DAY = date(2026, 1, 26)      # a Monday
NORMAL_TUESDAY = date(2026, 9, 1)
SATURDAY = date(2026, 9, 5)


class FakeBar:
    def __init__(self, when):
        self.timestamp = when


def test_fixed_holidays_skip_weekend_observances():
    """A fixed holiday landing on a weekend is not a separate closure."""
    for year in (2026, 2027, 2028):
        for when in fixed_holidays_for(year):
            assert when.weekday() < 5


def test_fixed_holidays_are_named():
    assert fixed_holidays_for(2026)[REPUBLIC_DAY] == "Republic Day"


def test_calendar_identifies_trading_days():
    calendar = HolidayCalendar().add_fixed_holidays(2026)
    assert not calendar.is_trading_day(REPUBLIC_DAY)     # holiday
    assert not calendar.is_trading_day(SATURDAY)         # weekend
    assert calendar.is_trading_day(NORMAL_TUESDAY)


def test_calendar_accepts_datetimes_as_well_as_dates():
    calendar = HolidayCalendar().add_fixed_holidays(2026)
    assert calendar.is_holiday(datetime(2026, 1, 26, 10, 0))
    assert calendar.is_trading_day(datetime(2026, 9, 1, 10, 0))


def test_next_trading_day_skips_holidays_and_weekends():
    calendar = HolidayCalendar({date(2026, 9, 2): "test holiday"})
    # Tue 1st -> Wed 2nd is a holiday -> Thu 3rd
    assert calendar.next_trading_day(date(2026, 9, 1)) == date(2026, 9, 3)
    # Fri 4th -> weekend -> Mon 7th
    assert calendar.next_trading_day(date(2026, 9, 4)) == date(2026, 9, 7)


def test_trading_days_between_excludes_closures():
    calendar = HolidayCalendar({date(2026, 9, 2): "test holiday"})
    days = calendar.trading_days_between(date(2026, 9, 1), date(2026, 9, 7))
    assert days == [date(2026, 9, 1), date(2026, 9, 3),
                    date(2026, 9, 4), date(2026, 9, 7)]


def test_derive_from_bars_finds_the_missing_weekday():
    """The authoritative path: a weekday with no candle was a closure."""
    bars = [FakeBar(date(2026, 9, 1)), FakeBar(date(2026, 9, 2)),
            FakeBar(date(2026, 9, 4))]          # 3rd missing
    derived = HolidayCalendar.derive_from_bars(bars, date(2026, 9, 1), date(2026, 9, 4))
    assert list(derived.to_dict()) == ["2026-09-03"]


def test_derive_from_bars_ignores_weekends():
    bars = [FakeBar(date(2026, 9, 4)), FakeBar(date(2026, 9, 7))]
    derived = HolidayCalendar.derive_from_bars(bars, date(2026, 9, 4), date(2026, 9, 7))
    assert len(derived) == 0        # the 5th and 6th are a weekend, not holidays


def test_derive_accepts_datetime_stamped_bars():
    bars = [FakeBar(datetime(2026, 9, 1, 9, 15)), FakeBar(datetime(2026, 9, 3, 9, 15))]
    derived = HolidayCalendar.derive_from_bars(bars, date(2026, 9, 1), date(2026, 9, 3))
    assert list(derived.to_dict()) == ["2026-09-02"]


def test_round_trip_through_disk(tmp_path):
    calendar = HolidayCalendar().add_fixed_holidays(2026)
    calendar.add(date(2026, 11, 9), "Diwali")
    path = tmp_path / "holidays.json"
    calendar.save(path)

    reloaded = HolidayCalendar.load(path)
    assert len(reloaded) == len(calendar)
    assert reloaded.name_for(date(2026, 11, 9)) == "Diwali"


def test_missing_file_gives_an_empty_calendar(tmp_path):
    assert len(HolidayCalendar.load(tmp_path / "nope.json")) == 0


def test_corrupt_file_is_ignored_not_fatal(tmp_path):
    path = tmp_path / "holidays.json"
    path.write_text("{not json")
    assert len(HolidayCalendar.load(path)) == 0


def test_bad_dates_inside_the_file_are_skipped(tmp_path):
    path = tmp_path / "holidays.json"
    path.write_text('{"2026-01-26": "Republic Day", "not-a-date": "junk"}')
    calendar = HolidayCalendar.load(path)
    assert len(calendar) == 1


# -- integration with the trading clock ---------------------------------------
def test_clock_blocks_entries_on_a_holiday(settings):
    calendar = HolidayCalendar().add_fixed_holidays(2026)
    clock = SessionClock(settings, calendar)
    holiday_morning = datetime(2026, 1, 26, 10, 0, tzinfo=IST)

    assert not clock.can_enter(holiday_morning)
    assert not clock.should_square_off(
        datetime(2026, 1, 26, 15, 20, tzinfo=IST)
    )
    assert "trading holiday" in clock.describe(holiday_morning)
    assert "Republic Day" in clock.describe(holiday_morning)


def test_clock_still_trades_on_a_normal_weekday(settings):
    clock = SessionClock(settings, HolidayCalendar().add_fixed_holidays(2026))
    assert clock.can_enter(datetime(2026, 9, 1, 10, 0, tzinfo=IST))


def test_clock_falls_back_to_weekends_only_without_a_calendar(settings):
    """No calendar must not crash — it just reverts to prior behaviour."""
    clock = SessionClock(settings, HolidayCalendar())
    assert clock.can_enter(datetime(2026, 1, 26, 10, 0, tzinfo=IST))
    assert not clock.can_enter(datetime(2026, 9, 5, 10, 0, tzinfo=IST))


def test_risk_manager_inherits_the_holiday_calendar(settings, infy):
    """Holidays must reach the risk gate, not just the clock."""
    from kitealgo.models import Side, Signal
    from kitealgo.portfolio import Portfolio
    from kitealgo.risk import RiskManager

    calendar = HolidayCalendar().add_fixed_holidays(2026)
    calendar.save(settings.holiday_file)

    risk = RiskManager(settings)
    risk.start_session(REPUBLIC_DAY)
    decision = risk.evaluate_entry(
        Signal(infy, Side.BUY, stop_loss=90.0), Portfolio(settings.risk.capital),
        100.0, datetime(2026, 1, 26, 10, 0, tzinfo=IST),
    )
    assert not decision.approved
    assert "outside entry window" in decision.reason
