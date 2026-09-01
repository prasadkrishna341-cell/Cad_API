"""Market session timing, in IST.

Everything time-related is centralised here so a strategy never has to reason
about timezones, and so tests can inject a fixed 'now' instead of waiting for
09:15 on a Tuesday.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime
from typing import Optional

from .config import IST, Settings

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
    """Answers the three questions the engine asks each loop."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def can_enter(self, moment: Optional[datetime] = None) -> bool:
        """Inside the window where new positions may be opened."""
        moment = as_ist(moment)
        if not is_weekday(moment):
            return False
        return self.settings.trade_start <= moment.time() < self.settings.trade_end

    def should_square_off(self, moment: Optional[datetime] = None) -> bool:
        """Past the intraday square-off time — flatten everything."""
        moment = as_ist(moment)
        if not is_weekday(moment):
            return False
        return moment.time() >= self.settings.square_off

    def is_session_over(self, moment: Optional[datetime] = None) -> bool:
        return as_ist(moment).time() >= MARKET_CLOSE

    def session_date(self, moment: Optional[datetime] = None) -> date:
        return as_ist(moment).date()

    def describe(self, moment: Optional[datetime] = None) -> str:
        moment = as_ist(moment)
        if not is_weekday(moment):
            return "weekend — market closed"
        if moment.time() < MARKET_OPEN:
            return f"pre-open (opens {MARKET_OPEN:%H:%M})"
        if self.should_square_off(moment):
            return "square-off window"
        if self.can_enter(moment):
            return "entries allowed"
        if moment.time() < MARKET_CLOSE:
            return "manage-only (no new entries)"
        return "market closed"
