"""NSE trading-holiday calendar.

The engine already skips weekends, but the exchange also closes for ~15 public
holidays a year.  Without them a session started on a holiday connects, waits,
and quietly does nothing — harmless but useless, and it makes backtest day
counts wrong.

**Where the dates come from matters.** Most Indian market holidays follow lunar
calendars and move every year, so this module does not ship a hardcoded guess at
them. Instead:

* `derive_from_bars()` reads the real calendar out of Kite's own daily candles —
  any weekday with no candle for a liquid index was a holiday. This is
  authoritative and self-updating, and is what `kitealgo.cli holidays --refresh`
  uses.
* The seed list below contains only *fixed-date* national holidays, which do not
  move. Anything lunar (Holi, Diwali, Eid, Muhurat trading...) must come from
  the refresh above or be added by hand.

Verify against NSE's official circular before relying on this for live trading:
https://www.nseindia.com/resources/exchange-communication-holidays
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

#: Fixed-date national holidays. These do not move year to year, so they are
#: safe to generate. Everything lunar is deliberately absent — see the module
#: docstring. (month, day, name)
FIXED_HOLIDAYS: list[tuple[int, int, str]] = [
    (1, 26, "Republic Day"),
    (5, 1, "Maharashtra Day"),
    (8, 15, "Independence Day"),
    (10, 2, "Gandhi Jayanti"),
    (12, 25, "Christmas"),
]


def fixed_holidays_for(year: int) -> dict[date, str]:
    """The fixed-date holidays for one year, weekends excluded."""
    out: dict[date, str] = {}
    for month, day, name in FIXED_HOLIDAYS:
        try:
            when = date(year, month, day)
        except ValueError:      # pragma: no cover - no invalid fixed dates today
            continue
        if when.weekday() < 5:  # a holiday falling on a weekend is not observed
            out[when] = name
    return out


class HolidayCalendar:
    """Knows which dates the exchange is closed."""

    def __init__(self, holidays: Optional[dict[date, str]] = None) -> None:
        self._holidays: dict[date, str] = dict(holidays or {})

    # -- queries ----------------------------------------------------------
    def __len__(self) -> int:
        return len(self._holidays)

    def __contains__(self, when: date) -> bool:
        return self.is_holiday(when)

    def is_holiday(self, when) -> bool:
        return self._as_date(when) in self._holidays

    def name_for(self, when) -> Optional[str]:
        return self._holidays.get(self._as_date(when))

    def is_trading_day(self, when) -> bool:
        """A weekday the exchange is actually open."""
        when = self._as_date(when)
        return when.weekday() < 5 and not self.is_holiday(when)

    def next_trading_day(self, when) -> date:
        when = self._as_date(when) + timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(when):
                return when
            when += timedelta(days=1)
        raise RuntimeError("No trading day found within 30 days — calendar looks wrong")

    def trading_days_between(self, start, end) -> list[date]:
        start, end = self._as_date(start), self._as_date(end)
        days, current = [], start
        while current <= end:
            if self.is_trading_day(current):
                days.append(current)
            current += timedelta(days=1)
        return days

    @staticmethod
    def _as_date(when) -> date:
        return when.date() if isinstance(when, datetime) else when

    # -- mutation ---------------------------------------------------------
    def add(self, when, name: str = "holiday") -> None:
        self._holidays[self._as_date(when)] = name

    def add_fixed_holidays(self, *years: int) -> "HolidayCalendar":
        for year in years:
            self._holidays.update(fixed_holidays_for(year))
        return self

    def to_dict(self) -> dict[str, str]:
        return {d.isoformat(): name for d, name in sorted(self._holidays.items())}

    # -- persistence ------------------------------------------------------
    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        log.info("Wrote %d holidays to %s", len(self._holidays), path)

    @classmethod
    def load(cls, path: Path) -> "HolidayCalendar":
        path = Path(path)
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            log.warning("Ignoring unreadable holiday file %s: %s", path, exc)
            return cls()
        holidays: dict[date, str] = {}
        for key, name in raw.items():
            try:
                holidays[date.fromisoformat(key)] = str(name)
            except ValueError:
                log.warning("Skipping bad holiday date %r in %s", key, path)
        return cls(holidays)

    # -- derivation from real market data ---------------------------------
    @classmethod
    def derive_from_bars(
        cls, bars: Iterable, from_date: date, to_date: date, name: str = "no trading"
    ) -> "HolidayCalendar":
        """Infer the calendar from daily candles.

        Any weekday inside the range with no candle is a day the exchange did
        not trade. Feed this daily candles for a liquid index (NIFTY 50) and it
        reproduces NSE's holiday list exactly, without anyone guessing dates.
        """
        traded = {
            (b.timestamp.date() if hasattr(b.timestamp, "date") else b.timestamp)
            for b in bars
        }
        holidays: dict[date, str] = {}
        current = from_date
        while current <= to_date:
            if current.weekday() < 5 and current not in traded:
                holidays[current] = name
            current += timedelta(days=1)
        return cls(holidays)
