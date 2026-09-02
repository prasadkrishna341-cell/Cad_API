"""Turn a stream of ticks into fixed-interval OHLCV candles.

Strategies reason in candles, the websocket speaks in ticks.  A bar is emitted
the moment a tick arrives belonging to a later interval, so a strategy never
sees a partially-formed candle as if it were closed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional

from ..models import Bar, Tick


def floor_time(moment: datetime, interval_seconds: int) -> datetime:
    """Snap a timestamp down to the start of its interval bucket."""
    seconds = moment.hour * 3600 + moment.minute * 60 + moment.second
    bucket = (seconds // interval_seconds) * interval_seconds
    return moment.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=bucket)


class BarBuilder:
    """Accumulates ticks for one instrument into candles."""

    def __init__(self, instrument_token: int, interval_seconds: int = 60) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.instrument_token = instrument_token
        self.interval_seconds = interval_seconds
        self.current: Optional[Bar] = None
        self._last_cumulative_volume: Optional[int] = None

    def add(self, tick: Tick) -> Optional[Bar]:
        """Feed a tick. Returns the previous bar if this tick closed it."""
        bucket = floor_time(tick.timestamp, self.interval_seconds)
        completed: Optional[Bar] = None

        # Kite reports volume cumulatively for the day; candles want the delta.
        volume_delta = 0
        if tick.volume:
            if self._last_cumulative_volume is not None:
                volume_delta = max(0, tick.volume - self._last_cumulative_volume)
            self._last_cumulative_volume = tick.volume

        if self.current is None:
            self.current = self._new_bar(bucket, tick, volume_delta)
            return None

        if bucket > self.current.timestamp:
            completed = self.current
            self.current = self._new_bar(bucket, tick, volume_delta)
            return completed

        if bucket < self.current.timestamp:
            return None  # a late/out-of-order tick — ignore rather than corrupt the bar

        bar = self.current
        bar.high = max(bar.high, tick.last_price)
        bar.low = min(bar.low, tick.last_price)
        bar.close = tick.last_price
        bar.volume += volume_delta
        return None

    def _new_bar(self, bucket: datetime, tick: Tick, volume: int) -> Bar:
        return Bar(
            timestamp=bucket,
            open=tick.last_price,
            high=tick.last_price,
            low=tick.last_price,
            close=tick.last_price,
            volume=volume,
            instrument_token=self.instrument_token,
        )

    def flush(self) -> Optional[Bar]:
        """Close and return the in-progress bar (end of session)."""
        bar, self.current = self.current, None
        return bar


class BarAggregator:
    """One `BarBuilder` per instrument, with a callback on each closed bar."""

    def __init__(
        self,
        interval_seconds: int = 60,
        on_bar: Optional[Callable[[Bar], None]] = None,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.on_bar = on_bar
        self._builders: dict[int, BarBuilder] = {}

    def add(self, tick: Tick) -> Optional[Bar]:
        builder = self._builders.get(tick.instrument_token)
        if builder is None:
            builder = BarBuilder(tick.instrument_token, self.interval_seconds)
            self._builders[tick.instrument_token] = builder
        bar = builder.add(tick)
        if bar is not None and self.on_bar is not None:
            self.on_bar(bar)
        return bar

    def add_many(self, ticks: Iterable[Tick]) -> list[Bar]:
        return [bar for tick in ticks if (bar := self.add(tick)) is not None]

    def current(self, instrument_token: int) -> Optional[Bar]:
        builder = self._builders.get(instrument_token)
        return builder.current if builder else None

    def flush_all(self) -> list[Bar]:
        bars = [bar for b in self._builders.values() if (bar := b.flush()) is not None]
        if self.on_bar:
            for bar in bars:
                self.on_bar(bar)
        return bars
