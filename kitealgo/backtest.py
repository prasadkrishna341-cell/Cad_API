"""Event-driven backtester.

Runs the *same* Strategy, RiskManager and Portfolio objects the live engine
uses, against historical candles and the PaperBroker.  That shared path is the
point: a strategy that backtests cannot behave differently live because of a
separate simulation code path.

Two honesty rules are enforced:

* Signals generated on a bar fill at the **next** bar's open, never that bar's
  close — otherwise you are trading on information you did not have.
* Stops and targets are checked against each bar's high/low, and if both could
  have been hit within one candle the **stop** is assumed first.  Intrabar
  order is unknowable from OHLC, so the pessimistic reading is the only safe one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Optional

from .broker.paper import PaperBroker
from .clock import as_ist
from .config import Settings
from .models import Bar, Fill, Instrument, Order, OrderType, Product, Side, Signal
from .portfolio import Portfolio
from .risk import RiskManager

log = logging.getLogger(__name__)


@dataclass
class Trade:
    """One completed round trip."""

    symbol: str
    side: str
    quantity: int
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    pnl: float = 0.0
    charges: float = 0.0
    exit_reason: str = ""

    @property
    def net_pnl(self) -> float:
        return self.pnl - self.charges

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    starting_capital: float = 0.0
    bars_processed: int = 0
    rejections: dict[str, int] = field(default_factory=dict)

    # -- metrics ----------------------------------------------------------
    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def gross_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def total_charges(self) -> float:
        return sum(t.charges for t in self.trades)

    @property
    def wins(self) -> list[Trade]:
        return [t for t in self.trades if t.is_win]

    @property
    def losses(self) -> list[Trade]:
        return [t for t in self.trades if not t.is_win]

    @property
    def win_rate(self) -> float:
        return len(self.wins) / len(self.trades) * 100 if self.trades else 0.0

    @property
    def average_win(self) -> float:
        return sum(t.net_pnl for t in self.wins) / len(self.wins) if self.wins else 0.0

    @property
    def average_loss(self) -> float:
        return sum(t.net_pnl for t in self.losses) / len(self.losses) if self.losses else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.net_pnl for t in self.wins)
        pain = abs(sum(t.net_pnl for t in self.losses))
        if pain == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / pain

    @property
    def expectancy(self) -> float:
        """Average net PnL per trade — the number that actually compounds."""
        return self.net_pnl / len(self.trades) if self.trades else 0.0

    @property
    def max_drawdown(self) -> float:
        """Largest peak-to-trough fall in equity, in currency."""
        peak = float("-inf")
        worst = 0.0
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
        return abs(worst)

    @property
    def max_drawdown_pct(self) -> float:
        return self.max_drawdown / self.starting_capital * 100 if self.starting_capital else 0.0

    @property
    def return_pct(self) -> float:
        return self.net_pnl / self.starting_capital * 100 if self.starting_capital else 0.0

    def summary(self) -> dict:
        return {
            "trades": len(self.trades),
            "wins": len(self.wins),
            "losses": len(self.losses),
            "win_rate_pct": round(self.win_rate, 2),
            "gross_pnl": round(self.gross_pnl, 2),
            "charges": round(self.total_charges, 2),
            "net_pnl": round(self.net_pnl, 2),
            "return_pct": round(self.return_pct, 2),
            "expectancy": round(self.expectancy, 2),
            "avg_win": round(self.average_win, 2),
            "avg_loss": round(self.average_loss, 2),
            "profit_factor": round(self.profit_factor, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "bars": self.bars_processed,
        }

    def format_report(self) -> str:
        s = self.summary()
        lines = [
            "",
            "=" * 58,
            "  BACKTEST RESULT",
            "=" * 58,
            f"  Bars processed     : {s['bars']:>12,}",
            f"  Trades             : {s['trades']:>12,}  ({s['wins']}W / {s['losses']}L)",
            f"  Win rate           : {s['win_rate_pct']:>11.2f}%",
            "  " + "-" * 54,
            f"  Gross PnL          : {s['gross_pnl']:>12,.2f}",
            f"  Charges            : {s['charges']:>12,.2f}",
            f"  Net PnL            : {s['net_pnl']:>12,.2f}",
            f"  Return on capital  : {s['return_pct']:>11.2f}%",
            "  " + "-" * 54,
            f"  Expectancy / trade : {s['expectancy']:>12,.2f}",
            f"  Average win        : {s['avg_win']:>12,.2f}",
            f"  Average loss       : {s['avg_loss']:>12,.2f}",
            f"  Profit factor      : {s['profit_factor']:>12,.2f}",
            f"  Max drawdown       : {s['max_drawdown']:>12,.2f}  ({s['max_drawdown_pct']:.2f}%)",
            "=" * 58,
        ]
        if self.rejections:
            lines.append("  Signals blocked by risk:")
            for reason, count in sorted(self.rejections.items(), key=lambda kv: -kv[1])[:6]:
                lines.append(f"    {count:>5} x {reason[:60]}")
            lines.append("=" * 58)
        return "\n".join(lines)


class Backtester:
    def __init__(
        self,
        strategy,
        settings: Settings,
        slippage_bps: float = 2.0,
        brokerage_rate: float = 0.0003,
        brokerage_cap: float = 20.0,
    ) -> None:
        self.strategy = strategy
        self.settings = settings
        self.broker = PaperBroker(
            slippage_bps=slippage_bps,
            brokerage_rate=brokerage_rate,
            brokerage_cap=brokerage_cap,
            starting_cash=settings.risk.capital,
        )
        self.portfolio = Portfolio(settings.risk.capital)
        self.risk = RiskManager(settings)
        self.result = BacktestResult(starting_capital=settings.risk.capital)
        self._open_trades: dict[int, Trade] = {}
        self._pending: list[Signal] = []
        self._session: Optional[date] = None
        # The portfolio zeroes realised PnL and charges at each new session so
        # the live engine's daily kill switch works. A backtest spans many
        # sessions, so campaign-level PnL has to be accumulated here or the
        # equity curve resets every morning and drawdown only ever measures
        # within one day.
        self._cumulative_realised = 0.0
        self._cumulative_charges = 0.0

    # -- helpers ----------------------------------------------------------
    def _reject(self, reason: str) -> None:
        key = reason.split("(")[0].strip()
        self.result.rejections[key] = self.result.rejections.get(key, 0) + 1

    def _charges(self, value: float) -> float:
        return self.broker.charges_for(value)

    def _execute(
        self, signal: Signal, price: float, quantity: int, when: datetime, reason: str
    ) -> None:
        instrument = signal.instrument
        self.broker.set_clock(when)
        self.broker.set_price(instrument, price)
        order = Order(
            instrument=instrument,
            side=signal.side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            product=Product(self.settings.default_product),
            tag=self.strategy.name[:20],
        )
        try:
            self.broker.place_order(order)
        except Exception as exc:
            log.debug("backtest order rejected: %s", exc)
            return
        if not order.is_complete:
            return

        fill = self.broker.fills()[-1]
        charges = self._charges(fill.value)
        realised = self.portfolio.apply_fill(fill, charges=charges)
        self._cumulative_realised += realised
        self._cumulative_charges += charges
        self._book_trade(fill, realised, charges, reason)

        position = self.portfolio.position(instrument)
        if position.is_open and not signal.is_exit:
            position.stop_loss = signal.stop_loss
            position.target = signal.target

    def _book_trade(self, fill: Fill, realised: float, charges: float, reason: str) -> None:
        token = fill.instrument.instrument_token
        trade = self._open_trades.get(token)
        if trade is None:
            self._open_trades[token] = Trade(
                symbol=fill.instrument.tradingsymbol,
                side=fill.side.value,
                quantity=fill.quantity,
                entry_time=fill.timestamp,
                entry_price=fill.price,
                charges=charges,
            )
            return
        trade.exit_time = fill.timestamp
        trade.exit_price = fill.price
        trade.pnl = realised
        trade.charges += charges
        trade.exit_reason = reason
        self.result.trades.append(trade)
        del self._open_trades[token]

    # -- main loop --------------------------------------------------------
    def run(self, bars: Iterable[Bar]) -> BacktestResult:
        bars = sorted(bars, key=lambda b: b.timestamp)
        warmup = self.strategy.warmup_bars
        self.risk.start_session(bars[0].timestamp.date() if bars else date.today())

        for index, bar in enumerate(bars):
            when = as_ist(bar.timestamp)
            session_day = when.date()

            if self._session != session_day:
                self._session = session_day
                self.portfolio.reset_day(session_day)
                self.risk.start_session(session_day)
                self.strategy.on_day_start(session_day)

            self.result.bars_processed += 1

            # 1. Fill orders queued on the previous bar, at this bar's open.
            self._fill_pending(bar, when)

            # 2. Mark to market, then check stops/targets against the bar's range.
            self.portfolio.mark_to_market({bar.instrument_token: bar.close})
            self._check_exits(bar, when)

            # 3. Let the strategy see the closed bar.
            if index >= warmup:
                position = self.portfolio.positions.get(bar.instrument_token)
                for signal in self.strategy.on_bar(bar, position):
                    self._pending.append(signal)

            self.result.equity_curve.append((when, self._equity()))

        self._close_out(bars[-1] if bars else None)
        return self.result

    def _equity(self) -> float:
        """Account value across the whole backtest, not just today."""
        return (
            self.settings.risk.capital
            + self._cumulative_realised
            - self._cumulative_charges
            + self.portfolio.unrealised_pnl
        )

    def _fill_pending(self, bar: Bar, when: datetime) -> None:
        """Signals from the previous bar execute at this bar's open."""
        pending, self._pending = self._pending, []
        for signal in pending:
            if signal.instrument.instrument_token != bar.instrument_token:
                self._pending.append(signal)  # different instrument; wait for its bar
                continue
            price = bar.open
            position = self.portfolio.positions.get(bar.instrument_token)

            if signal.is_exit:
                if position and position.is_open:
                    self._execute(signal, price, abs(position.quantity), when, signal.reason)
                continue

            decision = self.risk.evaluate_entry(signal, self.portfolio, price, when)
            if not decision:
                self._reject(decision.reason)
                continue
            self._execute(signal, price, decision.quantity, when, signal.reason)

    def _check_exits(self, bar: Bar, when: datetime) -> None:
        position = self.portfolio.positions.get(bar.instrument_token)
        if not position or not position.is_open:
            return

        exit_price: Optional[float] = None
        reason = ""

        # Pessimistic: if the bar's range covers both stop and target, assume
        # the stop filled first. OHLC cannot tell us which came first.
        if position.stop_loss:
            if position.is_long and bar.low <= position.stop_loss:
                exit_price, reason = position.stop_loss, "stop loss"
            elif position.is_short and bar.high >= position.stop_loss:
                exit_price, reason = position.stop_loss, "stop loss"

        if exit_price is None and position.target:
            if position.is_long and bar.high >= position.target:
                exit_price, reason = position.target, "target"
            elif position.is_short and bar.low <= position.target:
                exit_price, reason = position.target, "target"

        if exit_price is None and self.risk.clock.should_square_off(when):
            exit_price, reason = bar.close, "square-off"

        if exit_price is None:
            return

        signal = Signal(
            instrument=position.instrument,
            side=Side.SELL if position.is_long else Side.BUY,
            quantity=abs(position.quantity),
            is_exit=True,
            reason=reason,
        )
        self._execute(signal, exit_price, abs(position.quantity), when, reason)

    def _close_out(self, last_bar: Optional[Bar]) -> None:
        """Flatten anything still open at the end of the data."""
        if last_bar is None:
            return
        when = as_ist(last_bar.timestamp)
        for position in list(self.portfolio.open_positions):
            signal = Signal(
                instrument=position.instrument,
                side=Side.SELL if position.is_long else Side.BUY,
                quantity=abs(position.quantity),
                is_exit=True,
                reason="end of backtest",
            )
            self._execute(signal, last_bar.close, abs(position.quantity), when, "end of data")
