"""Opening Range Breakout.

The first `range_minutes` of the session define a high/low box.  A close beyond
the box is taken as the day's directional commitment: buy the upside break with
the stop at the box low, sell the downside break with the stop at the box high.
One trade per instrument per day.

A staple of Indian intraday trading, and a good fit for MIS product with the
engine's automatic square-off.
"""

from __future__ import annotations

import logging
from datetime import time as dtime
from typing import Optional

from ..clock import MARKET_OPEN, as_ist
from ..models import Bar, Position, Signal
from .base import Strategy

log = logging.getLogger(__name__)


class OpeningRangeBreakoutStrategy(Strategy):
    name = "orb"

    def __init__(
        self,
        instruments,
        range_minutes: int = 15,
        buffer_pct: float = 0.0005,
        reward_multiple: float = 1.5,
        allow_short: bool = True,
        session_open: dtime = MARKET_OPEN,
        **params,
    ) -> None:
        if range_minutes < 1:
            raise ValueError("range_minutes must be >= 1")
        self.range_minutes = range_minutes
        self.buffer_pct = buffer_pct
        self.reward_multiple = reward_multiple
        self.allow_short = allow_short
        self.session_open = session_open
        super().__init__(
            instruments,
            range_minutes=range_minutes, buffer_pct=buffer_pct,
            reward_multiple=reward_multiple, allow_short=allow_short, **params,
        )

    @property
    def warmup_bars(self) -> int:
        return 0  # the range is built fresh each morning

    def _minutes_since_open(self, bar: Bar) -> float:
        moment = as_ist(bar.timestamp)
        open_at = moment.replace(
            hour=self.session_open.hour, minute=self.session_open.minute,
            second=0, microsecond=0,
        )
        return (moment - open_at).total_seconds() / 60.0

    def on_bar(self, bar: Bar, position: Optional[Position] = None) -> list[Signal]:
        state = self.state.for_token(bar.instrument_token)
        instrument = next(
            (i for i in self.instruments if i.instrument_token == bar.instrument_token), None
        )
        if instrument is None:
            return []

        session_day = as_ist(bar.timestamp).date()
        if state.get("day_date") != session_day:
            # New session: rebuild the box.
            state.clear()
            state["day_date"] = session_day
            state["day_high"] = None
            state["day_low"] = None
            state["day_traded"] = False

        elapsed = self._minutes_since_open(bar)

        # Still inside the opening range: widen the box, take no trade.
        if elapsed < self.range_minutes:
            state["day_high"] = bar.high if state["day_high"] is None else max(state["day_high"], bar.high)
            state["day_low"] = bar.low if state["day_low"] is None else min(state["day_low"], bar.low)
            return []

        high, low = state.get("day_high"), state.get("day_low")
        if high is None or low is None or high <= low:
            return []  # no usable range (e.g. history started mid-session)

        if state.get("day_traded") or (position is not None and position.is_open):
            return []

        buffer = bar.close * self.buffer_pct
        box = high - low

        if bar.close > high + buffer:
            state["day_traded"] = True
            return [
                self.buy(
                    instrument,
                    reason=f"broke {self.range_minutes}m opening range high {high:.2f}",
                    stop_loss=round(low, 2),
                    target=round(bar.close + box * self.reward_multiple, 2),
                    timestamp=bar.timestamp,
                )
            ]

        if self.allow_short and bar.close < low - buffer:
            state["day_traded"] = True
            return [
                self.sell(
                    instrument,
                    reason=f"broke {self.range_minutes}m opening range low {low:.2f}",
                    stop_loss=round(high, 2),
                    target=round(bar.close - box * self.reward_multiple, 2),
                    timestamp=bar.timestamp,
                )
            ]

        return []
