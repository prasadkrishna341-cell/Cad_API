"""Incremental indicators.

Each one updates in O(1) per bar rather than recomputing over a window, so the
same objects can drive a backtest over years of candles and a live session
without a rewrite. `value` is None until enough bars have been seen — callers
must check, which stops a strategy trading off a half-warmed indicator.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from .models import Bar


class Indicator:
    """Common shape: feed a number, read `.value`, check `.ready`."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self.value: Optional[float] = None
        self.count = 0

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, value: float) -> Optional[float]:
        raise NotImplementedError


class SMA(Indicator):
    """Simple moving average."""

    def __init__(self, period: int) -> None:
        super().__init__(period)
        self._window: deque[float] = deque(maxlen=period)
        self._sum = 0.0

    def update(self, value: float) -> Optional[float]:
        self.count += 1
        if len(self._window) == self.period:
            self._sum -= self._window[0]
        self._window.append(value)
        self._sum += value
        if len(self._window) == self.period:
            self.value = self._sum / self.period
        return self.value


class EMA(Indicator):
    """Exponential moving average, seeded with an SMA of the first `period` bars."""

    def __init__(self, period: int) -> None:
        super().__init__(period)
        self.multiplier = 2.0 / (period + 1)
        self._seed_sum = 0.0

    def update(self, value: float) -> Optional[float]:
        self.count += 1
        if self.value is None:
            self._seed_sum += value
            if self.count == self.period:
                self.value = self._seed_sum / self.period
            return self.value
        self.value = (value - self.value) * self.multiplier + self.value
        return self.value


class RSI(Indicator):
    """Wilder's relative strength index."""

    def __init__(self, period: int = 14) -> None:
        super().__init__(period)
        self._prev: Optional[float] = None
        self._avg_gain = 0.0
        self._avg_loss = 0.0

    def update(self, value: float) -> Optional[float]:
        if self._prev is None:
            self._prev = value
            return None
        change = value - self._prev
        self._prev = value
        gain, loss = max(change, 0.0), max(-change, 0.0)
        self.count += 1

        if self.count < self.period:
            self._avg_gain += gain
            self._avg_loss += loss
            return None
        if self.count == self.period:
            self._avg_gain = (self._avg_gain + gain) / self.period
            self._avg_loss = (self._avg_loss + loss) / self.period
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

        if self._avg_loss == 0:
            self.value = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self.value = 100.0 - (100.0 / (1.0 + rs))
        return self.value


class ATR(Indicator):
    """Average true range — the volatility measure used for stop distances."""

    def __init__(self, period: int = 14) -> None:
        super().__init__(period)
        self._prev_close: Optional[float] = None
        self._seed_sum = 0.0

    def update_bar(self, bar: Bar) -> Optional[float]:
        if self._prev_close is None:
            true_range = bar.high - bar.low
        else:
            true_range = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._prev_close = bar.close
        self.count += 1

        if self.value is None:
            self._seed_sum += true_range
            if self.count == self.period:
                self.value = self._seed_sum / self.period
            return self.value
        # Wilder smoothing.
        self.value = (self.value * (self.period - 1) + true_range) / self.period
        return self.value

    def update(self, value: float) -> Optional[float]:
        raise TypeError("ATR needs a full bar — call update_bar(bar) instead")


class Crossover:
    """Detects when one series crosses another. Emits only on the crossing bar."""

    def __init__(self) -> None:
        self._was_above: Optional[bool] = None

    def update(self, fast: Optional[float], slow: Optional[float]) -> Optional[str]:
        """Returns 'up', 'down', or None."""
        if fast is None or slow is None:
            return None
        is_above = fast > slow
        previous, self._was_above = self._was_above, is_above
        if previous is None or previous == is_above:
            return None
        return "up" if is_above else "down"
