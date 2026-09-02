"""EMA crossover with an ATR stop.

Long when the fast EMA crosses above the slow one, flat (or short, if enabled)
when it crosses back.  The stop is placed `atr_multiple` ATRs away, so position
size adapts to volatility rather than being a fixed number of shares.

This is a trend-following template, included because it is the clearest example
of the framework's shape — not because it is a money-maker. Backtest it on your
own instruments before it ever sees real capital.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..indicators import ATR, EMA, Crossover
from ..models import Bar, Position, Signal
from .base import Strategy

log = logging.getLogger(__name__)


class EmaCrossoverStrategy(Strategy):
    name = "ema_cross"

    def __init__(
        self,
        instruments,
        fast_period: int = 9,
        slow_period: int = 21,
        atr_period: int = 14,
        atr_multiple: float = 2.0,
        reward_multiple: float = 2.0,
        allow_short: bool = False,
        exit_on_cross: bool = True,
        **params,
    ) -> None:
        if fast_period >= slow_period:
            raise ValueError(
                f"fast_period ({fast_period}) must be less than slow_period ({slow_period})"
            )
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.atr_period = atr_period
        self.atr_multiple = atr_multiple
        self.reward_multiple = reward_multiple
        self.allow_short = allow_short
        self.exit_on_cross = exit_on_cross
        super().__init__(
            instruments,
            fast_period=fast_period, slow_period=slow_period, atr_period=atr_period,
            atr_multiple=atr_multiple, reward_multiple=reward_multiple,
            allow_short=allow_short, exit_on_cross=exit_on_cross, **params,
        )

    def _setup(self) -> None:
        for instrument in self.instruments:
            self.state.for_token(instrument.instrument_token).update(
                fast=EMA(self.fast_period),
                slow=EMA(self.slow_period),
                atr=ATR(self.atr_period),
                cross=Crossover(),
            )

    @property
    def warmup_bars(self) -> int:
        return max(self.slow_period, self.atr_period) + 1

    def on_bar(self, bar: Bar, position: Optional[Position] = None) -> list[Signal]:
        state = self.state.for_token(bar.instrument_token)
        if not state:
            return []  # a bar for an instrument this strategy doesn't trade

        fast = state["fast"].update(bar.close)
        slow = state["slow"].update(bar.close)
        atr = state["atr"].update_bar(bar)
        direction = state["cross"].update(fast, slow)

        if direction is None or atr is None or not atr:
            return []

        instrument = next(
            (i for i in self.instruments if i.instrument_token == bar.instrument_token), None
        )
        if instrument is None:
            return []

        holding = position is not None and position.is_open
        stop_distance = atr * self.atr_multiple

        signals: list[Signal] = []

        if direction == "up":
            # Close a short first; the engine applies exits before entries, so
            # emitting both here reverses the position on this bar.
            if holding and position.is_short:
                if not self.exit_on_cross:
                    return []       # let the stop or target close it instead
                signals.append(self.exit(position, "EMA crossed up while short"))
            elif holding:
                return []  # already long, nothing to do
            signals.append(
                self.buy(
                    instrument,
                    reason=f"EMA{self.fast_period} crossed above EMA{self.slow_period}",
                    stop_loss=round(bar.close - stop_distance, 2),
                    target=round(bar.close + stop_distance * self.reward_multiple, 2),
                    timestamp=bar.timestamp,
                )
            )
            return signals

        # direction == "down"
        if holding and position.is_long:
            if not self.exit_on_cross:
                return []           # let the stop or target close it instead
            signals.append(self.exit(position, "EMA crossed down while long"))
        elif holding:
            return []  # already short
        if not self.allow_short:
            return signals  # long-only: exit and stand aside
        signals.append(
            self.sell(
                instrument,
                reason=f"EMA{self.fast_period} crossed below EMA{self.slow_period}",
                stop_loss=round(bar.close + stop_distance, 2),
                target=round(bar.close - stop_distance * self.reward_multiple, 2),
                timestamp=bar.timestamp,
            )
        )
        return signals
