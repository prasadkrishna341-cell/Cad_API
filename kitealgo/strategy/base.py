"""The strategy contract.

A strategy answers one question — *should I be long, short, or flat?* — and
says why.  It deliberately cannot size a position, place an order, or know its
own PnL: that separation is what lets the same class run untouched in a
backtest and in a live session, and it keeps the risk manager authoritative.

Implement `on_bar`. Return `Signal`s; return `[]` to do nothing.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Iterable, Optional

from ..models import Bar, Instrument, Position, Signal, Tick

log = logging.getLogger(__name__)


class StrategyState:
    """Per-instrument scratch space (indicators, counters, flags)."""

    def __init__(self) -> None:
        self._data: dict[int, dict[str, Any]] = {}

    def for_token(self, token: int) -> dict[str, Any]:
        return self._data.setdefault(int(token), {})

    def clear(self) -> None:
        self._data.clear()

    def clear_day(self) -> None:
        """Drop only keys flagged as intraday-only (prefixed 'day_')."""
        for state in self._data.values():
            for key in [k for k in state if k.startswith("day_")]:
                del state[key]


class Strategy(ABC):
    #: Shown in logs and written into the order tag (Kite caps tags at 20 chars).
    name: str = "strategy"

    def __init__(self, instruments: Iterable[Instrument], **params: Any) -> None:
        self.instruments = list(instruments)
        self.params = params
        self.state = StrategyState()
        self.tokens = {i.instrument_token for i in self.instruments}
        self._setup()

    def _setup(self) -> None:
        """Build per-instrument indicators. Called once at construction."""

    @property
    def warmup_bars(self) -> int:
        """Bars needed before signals are trustworthy. The backtester skips these."""
        return 0

    @abstractmethod
    def on_bar(self, bar: Bar, position: Optional[Position] = None) -> list[Signal]:
        """Called once per closed candle."""

    def on_tick(self, tick: Tick, position: Optional[Position] = None) -> list[Signal]:
        """Called per tick. Default: do nothing — most strategies work on bars."""
        return []

    def on_day_start(self, session_date) -> None:
        """New trading day. Clears intraday state by default."""
        self.state.clear_day()

    # -- helpers for subclasses ------------------------------------------
    def buy(self, instrument: Instrument, **kwargs) -> Signal:
        from ..models import Side
        return Signal(instrument=instrument, side=Side.BUY, **kwargs)

    def sell(self, instrument: Instrument, **kwargs) -> Signal:
        from ..models import Side
        return Signal(instrument=instrument, side=Side.SELL, **kwargs)

    def exit(self, position: Position, reason: str) -> Signal:
        """Close an open position, whichever way it points."""
        from ..models import Side
        return Signal(
            instrument=position.instrument,
            side=Side.SELL if position.is_long else Side.BUY,
            quantity=abs(position.quantity),
            reason=reason,
            is_exit=True,
        )

    def __repr__(self) -> str:
        symbols = ", ".join(i.tradingsymbol for i in self.instruments[:3])
        more = "..." if len(self.instruments) > 3 else ""
        return f"<{type(self).__name__} [{symbols}{more}] {self.params}>"
