"""Market session timing, in IST.

Everything time-related is centralised here so a strategy never has to reason
about timezones, and so tests can inject a fixed 'now' instead of waiting for
09:15 on a Tuesday.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from typing import Optional

from .config import IST, Settings
from .holidays import HolidayCalendar

# NSE/BSE regular equity session.
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


def now_ist() -> datetime:
    return datetime.now(IST)


def as_ist(moment: Optional[datetime] = None) -> datetime:
    if moment is None:
        return now_ist()
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def is_weekday(moment: Optional[datetime] = None) -> bool:
    return as_ist(moment).weekday() < 5


def is_market_open(moment: Optional[datetime] = None) -> bool:
    """True during the regular equity session. Does not know about holidays."""
    moment = as_ist(moment)
    return is_weekday(moment) and MARKET_OPEN <= moment.time() <= MARKET_CLOSE


class SessionClock:
    """Answers the three questions the engine asks each loop.

    A `HolidayCalendar` may be supplied; when omitted, one is loaded from the
    state directory if present. With no calendar at all the clock falls back to
    weekends-only, which is what it did before holidays existed.
    """

    def __init__(
        self, settings: Settings, holidays: Optional[HolidayCalendar] = None
    ) -> None:
        self.settings = settings
        self.holidays = (
            holidays if holidays is not None
            else HolidayCalendar.load(settings.holiday_file)
        )

    def is_trading_day(self, moment: Optional[datetime] = None) -> bool:
        """A weekday the exchange is actually open."""
        return self.holidays.is_trading_day(as_ist(moment).date())

    def can_enter(self, moment: Optional[datetime] = None) -> bool:
        """Inside the window where new positions may be opened."""
        moment = as_ist(moment)
        if not self.is_trading_day(moment):
            return False
        return self.settings.trade_start <= moment.time() < self.settings.trade_end

    def should_square_off(self, moment: Optional[datetime] = None) -> bool:
        """Past the intraday square-off time — flatten everything."""
        moment = as_ist(moment)
        if not self.is_trading_day(moment):
            return False
        return moment.time() >= self.settings.square_off

    def is_session_over(self, moment: Optional[datetime] = None) -> bool:
        return as_ist(moment).time() >= MARKET_CLOSE

    def session_date(self, moment: Optional[datetime] = None) -> date:
        return as_ist(moment).date()

    def last_completed_session(self, moment: Optional[datetime] = None) -> date:
        """The most recent date whose trading session has finished.

        Today counts only once the market has closed. Anything earlier walks
        back past weekends and holidays. Backtests default to ending here so
        they never include a candle that is still forming — otherwise the same
        command gives slightly different numbers each time it is run, and
        comparing two parameter sets becomes guesswork.
        """
        moment = as_ist(moment)
        candidate = moment.date()
        if not (
            self.holidays.is_trading_day(candidate) and moment.time() >= MARKET_CLOSE
        ):
            candidate -= timedelta(days=1)
        for _ in range(30):
            if self.holidays.is_trading_day(candidate):
                return candidate
            candidate -= timedelta(days=1)
        raise RuntimeError("No completed session found in the last 30 days")

    def describe(self, moment: Optional[datetime] = None) -> str:
        moment = as_ist(moment)
        if not is_weekday(moment):
            return "weekend — market closed"
        if self.holidays.is_holiday(moment.date()):
            return f"trading holiday ({self.holidays.name_for(moment.date())})"
        if moment.time() < MARKET_OPEN:
            return f"pre-open (opens {MARKET_OPEN:%H:%M})"
        if self.should_square_off(moment):
            return "square-off window"
        if self.can_enter(moment):
            return "entries allowed"
        if moment.time() < MARKET_CLOSE:
            return "manage-only (no new entries)"
        return "market closed"
