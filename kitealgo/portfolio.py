"""Tracks positions, marks them to market, and reports PnL."""

from __future__ import annotations

import logging
from datetime import date
from typing import Iterable, Optional

from .models import Fill, Instrument, Position

log = logging.getLogger(__name__)


class Portfolio:
    """Net positions per instrument plus day-level PnL bookkeeping."""

    def __init__(self, starting_capital: float = 0.0) -> None:
        self.starting_capital = starting_capital
        self.positions: dict[int, Position] = {}
        self.fills: list[Fill] = []
        self.charges = 0.0
        self.trades_today = 0
        self.session_date: Optional[date] = None

    # -- mutation ---------------------------------------------------------
    def position(self, instrument: Instrument) -> Position:
        return self.positions.setdefault(
            instrument.instrument_token, Position(instrument=instrument)
        )

    def apply_fill(self, fill: Fill, charges: float = 0.0) -> float:
        """Book a fill. Returns realised PnL from it."""
        position = self.position(fill.instrument)
        was_flat = not position.is_open
        realised = position.apply_fill(fill)
        self.fills.append(fill)
        self.charges += charges
        if was_flat and position.is_open:
            self.trades_today += 1
        return realised

    def mark_to_market(self, prices: dict[int, float]) -> None:
        for token, price in prices.items():
            position = self.positions.get(token)
            if position is not None:
                position.last_price = price

    def reset_day(self, session_date: date) -> None:
        """Roll the day counters. Open positions carry over; day stats do not."""
        self.session_date = session_date
        self.trades_today = 0
        self.charges = 0.0
        for position in self.positions.values():
            position.realised_pnl = 0.0

    # -- reporting --------------------------------------------------------
    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    @property
    def open_count(self) -> int:
        return len(self.open_positions)

    @property
    def realised_pnl(self) -> float:
        return sum(p.realised_pnl for p in self.positions.values())

    @property
    def unrealised_pnl(self) -> float:
        return sum(p.unrealised_pnl for p in self.positions.values())

    @property
    def total_pnl(self) -> float:
        """Net of charges — the number that decides whether the kill switch trips."""
        return self.realised_pnl + self.unrealised_pnl - self.charges

    @property
    def gross_exposure(self) -> float:
        return sum(p.exposure for p in self.open_positions)

    def exposure_in(self, instrument: Instrument) -> float:
        position = self.positions.get(instrument.instrument_token)
        return position.exposure if position else 0.0

    def has_position(self, instrument: Instrument) -> bool:
        position = self.positions.get(instrument.instrument_token)
        return bool(position and position.is_open)

    def summary(self) -> dict:
        return {
            "open_positions": self.open_count,
            "trades_today": self.trades_today,
            "realised_pnl": round(self.realised_pnl, 2),
            "unrealised_pnl": round(self.unrealised_pnl, 2),
            "charges": round(self.charges, 2),
            "total_pnl": round(self.total_pnl, 2),
            "gross_exposure": round(self.gross_exposure, 2),
        }

    def describe_positions(self) -> list[dict]:
        return [
            {
                "symbol": p.instrument.tradingsymbol,
                "qty": p.quantity,
                "avg": round(p.average_price, 2),
                "ltp": round(p.last_price, 2),
                "pnl": round(p.total_pnl, 2),
                "sl": p.stop_loss,
                "target": p.target,
            }
            for p in self.open_positions
        ]
