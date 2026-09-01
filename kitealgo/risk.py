"""The risk layer — the last thing between a strategy and your capital.

Every entry passes through :meth:`RiskManager.evaluate_entry`, which either
returns an approved quantity or a reason for refusal.  Strategies cannot
bypass it: the engine sizes and vetoes, strategies only express intent.

The checks, in the order they run:

1. the daily-loss kill switch (once tripped, no new entries for the session)
2. the entry time window
3. max simultaneous open positions
4. max entries per day
5. position sizing from stop distance, capped by max position value
6. lot-size rounding (F&O must trade in whole lots)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from .clock import SessionClock, as_ist
from .config import RiskLimits, Settings
from .models import Instrument, Side, Signal
from .portfolio import Portfolio

log = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    """The verdict on one proposed entry."""

    approved: bool
    quantity: int = 0
    reason: str = ""

    def __bool__(self) -> bool:
        return self.approved

    @classmethod
    def reject(cls, reason: str) -> "RiskDecision":
        return cls(approved=False, quantity=0, reason=reason)

    @classmethod
    def accept(cls, quantity: int, reason: str = "ok") -> "RiskDecision":
        return cls(approved=True, quantity=quantity, reason=reason)


class RiskManager:
    def __init__(self, settings: Settings, limits: Optional[RiskLimits] = None) -> None:
        self.settings = settings
        self.limits = limits or settings.risk
        self.clock = SessionClock(settings)
        self.halted = False
        self.halt_reason = ""
        self.session_date: Optional[date] = None

    # -- kill switch ------------------------------------------------------
    def start_session(self, session_date: date) -> None:
        """Begin a fresh trading day: counters zeroed, kill switch re-armed."""
        self.session_date = session_date
        self.halted = False
        self.halt_reason = ""

    def halt(self, reason: str) -> None:
        if not self.halted:
            log.error("TRADING HALTED: %s", reason)
        self.halted = True
        self.halt_reason = reason

    def check_daily_loss(self, portfolio: Portfolio) -> bool:
        """Trip the kill switch if the day's loss has breached the limit.

        Returns True when trading is halted.
        """
        loss = -portfolio.total_pnl
        if loss >= self.limits.max_loss_amount:
            self.halt(
                f"daily loss {loss:,.2f} reached limit {self.limits.max_loss_amount:,.2f}"
            )
        return self.halted

    # -- sizing -----------------------------------------------------------
    def size_position(
        self,
        entry_price: float,
        stop_loss: Optional[float],
        lot_size: int = 1,
    ) -> tuple[int, str]:
        """Quantity to trade, and how it was arrived at.

        With a stop, size so that being stopped out costs exactly
        `risk_per_trade` of capital.  Without one, fall back to the max
        position value — a strategy that doesn't define risk gets the
        conservative treatment, not a bigger position.
        """
        if entry_price <= 0:
            return 0, "entry price must be positive"

        cap_by_value = self.limits.max_position_value / entry_price

        if stop_loss and stop_loss > 0:
            risk_per_share = abs(entry_price - stop_loss)
            if risk_per_share <= 0:
                return 0, "stop loss equals entry price"
            by_risk = self.limits.risk_amount_per_trade / risk_per_share
            raw = min(by_risk, cap_by_value)
            basis = (
                f"risk {self.limits.risk_amount_per_trade:,.0f} / {risk_per_share:.2f} per share"
                if by_risk <= cap_by_value
                else f"capped at max position value {self.limits.max_position_value:,.0f}"
            )
        else:
            raw = cap_by_value
            basis = f"no stop loss — capped at {self.limits.max_position_value:,.0f} notional"

        # F&O trades in whole lots; equities have lot_size 1.
        lot_size = max(1, lot_size)
        quantity = int(math.floor(raw / lot_size) * lot_size)
        if quantity <= 0:
            return 0, (
                f"sized to zero ({basis}); capital {self.limits.capital:,.0f} is too "
                f"small for one lot of {lot_size} at {entry_price:,.2f}"
            )
        return quantity, basis

    # -- entry gate -------------------------------------------------------
    def evaluate_entry(
        self,
        signal: Signal,
        portfolio: Portfolio,
        price: float,
        now: Optional[datetime] = None,
    ) -> RiskDecision:
        now = as_ist(now)
        instrument = signal.instrument

        if self.halted:
            return RiskDecision.reject(f"trading halted: {self.halt_reason}")

        if self.check_daily_loss(portfolio):
            return RiskDecision.reject(f"trading halted: {self.halt_reason}")

        if not self.clock.can_enter(now):
            return RiskDecision.reject(
                f"outside entry window "
                f"{self.settings.trade_start:%H:%M}-{self.settings.trade_end:%H:%M} "
                f"({self.clock.describe(now)})"
            )

        if portfolio.has_position(instrument):
            return RiskDecision.reject(f"already holding {instrument.tradingsymbol}")

        if portfolio.open_count >= self.limits.max_open_positions:
            return RiskDecision.reject(
                f"max open positions reached ({self.limits.max_open_positions})"
            )

        if portfolio.trades_today >= self.limits.max_trades_per_day:
            return RiskDecision.reject(
                f"max trades per day reached ({self.limits.max_trades_per_day})"
            )

        if signal.quantity:
            quantity, basis = signal.quantity, "strategy-specified"
            if quantity * price > self.limits.max_position_value:
                return RiskDecision.reject(
                    f"requested {quantity} x {price:,.2f} exceeds max position value "
                    f"{self.limits.max_position_value:,.2f}"
                )
        else:
            quantity, basis = self.size_position(price, signal.stop_loss, instrument.lot_size)

        if quantity <= 0:
            return RiskDecision.reject(basis)

        # Sanity check the stop's direction — a stop on the wrong side of entry
        # is a strategy bug that would otherwise fire instantly.
        if signal.stop_loss:
            if signal.side is Side.BUY and signal.stop_loss >= price:
                return RiskDecision.reject(
                    f"long stop {signal.stop_loss:,.2f} is at or above entry {price:,.2f}"
                )
            if signal.side is Side.SELL and signal.stop_loss <= price:
                return RiskDecision.reject(
                    f"short stop {signal.stop_loss:,.2f} is at or below entry {price:,.2f}"
                )

        return RiskDecision.accept(quantity, basis)

    # -- exits ------------------------------------------------------------
    def should_exit(
        self, position, price: float, now: Optional[datetime] = None
    ) -> Optional[str]:
        """Reason to close an open position now, or None to hold."""
        if not position.is_open:
            return None
        if self.clock.should_square_off(now):
            return "square-off time"
        if position.stop_loss:
            if position.is_long and price <= position.stop_loss:
                return f"stop loss hit at {price:,.2f}"
            if position.is_short and price >= position.stop_loss:
                return f"stop loss hit at {price:,.2f}"
        if position.target:
            if position.is_long and price >= position.target:
                return f"target hit at {price:,.2f}"
            if position.is_short and price <= position.target:
                return f"target hit at {price:,.2f}"
        return None

    def status(self, portfolio: Portfolio) -> dict:
        return {
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "daily_loss_limit": round(self.limits.max_loss_amount, 2),
            "current_pnl": round(portfolio.total_pnl, 2),
            "open_positions": f"{portfolio.open_count}/{self.limits.max_open_positions}",
            "trades_today": f"{portfolio.trades_today}/{self.limits.max_trades_per_day}",
        }
