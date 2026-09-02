"""Historical candles from Kite, chunked and cached.

Kite caps how far back a single `historical_data` call may reach, and the cap
depends on the interval.  Ask for more and the call simply errors, so requests
are split into legal windows and stitched back together.

Note: historical data requires the paid historical-data add-on on your Kite
Connect app. Without it these calls return a permission error.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from ..config import Settings
from ..models import Bar, Instrument
from ..ratelimit import HISTORICAL_LIMITER

log = logging.getLogger(__name__)

#: Maximum days of history Kite returns per request, per interval.
INTERVAL_MAX_DAYS: dict[str, int] = {
    "minute": 60,
    "3minute": 100,
    "5minute": 100,
    "10minute": 100,
    "15minute": 200,
    "30minute": 200,
    "60minute": 400,
    "day": 2000,
}

INTERVAL_SECONDS: dict[str, int] = {
    "minute": 60,
    "3minute": 180,
    "5minute": 300,
    "10minute": 600,
    "15minute": 900,
    "30minute": 1800,
    "60minute": 3600,
    "day": 86400,
}


def chunk_ranges(
    from_date: date, to_date: date, interval: str
) -> list[tuple[date, date]]:
    """Split a date range into windows Kite will actually serve."""
    if interval not in INTERVAL_MAX_DAYS:
        raise ValueError(
            f"Unknown interval {interval!r}. Valid: {', '.join(sorted(INTERVAL_MAX_DAYS))}"
        )
    if from_date > to_date:
        raise ValueError("from_date must not be after to_date")

    span = INTERVAL_MAX_DAYS[interval]
    chunks: list[tuple[date, date]] = []
    start = from_date
    while start <= to_date:
        end = min(start + timedelta(days=span - 1), to_date)
        chunks.append((start, end))
        start = end + timedelta(days=1)
    return chunks


def _to_bar(row: dict, token: int) -> Bar:
    return Bar(
        timestamp=row["date"],
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=int(row.get("volume") or 0),
        instrument_token=token,
    )


class HistoricalData:
    """Fetches candles, transparently caching each (instrument, interval, range)."""

    def __init__(self, kite, settings: Settings, use_cache: bool = True) -> None:
        self.kite = kite
        self.settings = settings
        self.use_cache = use_cache

    def _cache_path(self, token: int, interval: str, start: date, end: date):
        name = f"hist_{token}_{interval}_{start.isoformat()}_{end.isoformat()}.json"
        return self.settings.cache_dir / name

    def fetch(
        self,
        instrument: Instrument,
        from_date: date,
        to_date: date,
        interval: str = "5minute",
        oi: bool = False,
    ) -> list[Bar]:
        """Return candles for the range, in ascending time order."""
        token = instrument.instrument_token
        bars: list[Bar] = []

        for start, end in chunk_ranges(from_date, to_date, interval):
            rows = self._fetch_chunk(token, start, end, interval, oi)
            bars.extend(_to_bar(row, token) for row in rows)

        # Chunk boundaries can overlap by a candle; de-duplicate on timestamp.
        seen: set = set()
        unique: list[Bar] = []
        for bar in sorted(bars, key=lambda b: b.timestamp):
            if bar.timestamp not in seen:
                seen.add(bar.timestamp)
                unique.append(bar)
        log.info(
            "Fetched %d %s candles for %s (%s..%s)",
            len(unique), interval, instrument.tradingsymbol, from_date, to_date,
        )
        return unique

    def _fetch_chunk(
        self, token: int, start: date, end: date, interval: str, oi: bool
    ) -> list[dict]:
        path = self._cache_path(token, interval, start, end)
        # Only ranges that are entirely in the past are safe to cache — today's
        # candles are still being written.
        cacheable = self.use_cache and end < date.today()

        if cacheable and path.is_file():
            try:
                raw = json.loads(path.read_text())
                for row in raw:
                    row["date"] = datetime.fromisoformat(row["date"])
                return raw
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                log.warning("Discarding bad cache file %s: %s", path, exc)

        HISTORICAL_LIMITER.acquire()
        try:
            rows = self.kite.historical_data(
                instrument_token=token,
                from_date=start,
                to_date=end,
                interval=interval,
                oi=oi,
            )
        except Exception as exc:
            raise RuntimeError(
                f"historical_data failed for token={token} {start}..{end} "
                f"({interval}): {exc}. Note that historical data needs the paid "
                "add-on on your Kite Connect app."
            ) from exc

        if cacheable and rows:
            self.settings.ensure_dirs()
            path.write_text(json.dumps(
                [{**row, "date": row["date"].isoformat()} for row in rows], default=str
            ))
        return rows

    def fetch_days(
        self, instrument: Instrument, days: int, interval: str = "5minute"
    ) -> list[Bar]:
        """Convenience: the last `days` calendar days of candles."""
        today = date.today()
        return self.fetch(instrument, today - timedelta(days=days), today, interval)

    @staticmethod
    def to_dataframe(bars: list[Bar]):
        """Candles as a pandas DataFrame indexed by timestamp."""
        import pandas as pd

        return pd.DataFrame(
            [
                {
                    "timestamp": b.timestamp, "open": b.open, "high": b.high,
                    "low": b.low, "close": b.close, "volume": b.volume,
                }
                for b in bars
            ]
        ).set_index("timestamp")
