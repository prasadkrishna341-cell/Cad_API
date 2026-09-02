"""Command line interface.

    python -m kitealgo.cli login
    python -m kitealgo.cli instruments --search RELIANCE
    python -m kitealgo.cli backtest --symbols INFY,TCS --days 60 --strategy ema_crossover
    python -m kitealgo.cli run --symbols INFY --strategy orb
    python -m kitealgo.cli status
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta

from .config import ConfigError, Settings
from .logging_setup import setup_logging

log = logging.getLogger("kitealgo.cli")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _parse_params(pairs: list[str]) -> dict:
    """Turn `--param fast_period=9 --param allow_short=true` into kwargs."""
    params: dict = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--param expects key=value, got {pair!r}")
        key, _, raw = pair.partition("=")
        value: object = raw
        if raw.lower() in ("true", "false"):
            value = raw.lower() == "true"
        else:
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    pass
        params[key.strip()] = value
    return params


def _resolve_instruments(settings: Settings, symbols: list[str], exchange: str, kite=None):
    from .instruments import InstrumentMaster

    master = InstrumentMaster(settings, kite=kite).load()
    return master.resolve_all(symbols, exchange)


def _banner(settings: Settings) -> None:
    mode = settings.mode_label
    marker = "!!! REAL MONEY !!!" if settings.is_live else "simulated orders only"
    print(f"\n  kitealgo — mode: {mode}  ({marker})")
    print(f"  capital {settings.risk.capital:,.0f} | risk/trade "
          f"{settings.risk.risk_per_trade:.1%} | max daily loss "
          f"{settings.risk.max_daily_loss_pct:.1%} ({settings.risk.max_loss_amount:,.0f})\n")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_login(args, settings: Settings) -> int:
    from .auth import TokenStore, interactive_login

    if args.force:
        TokenStore(settings).clear()
        print("Cleared cached token.")
    interactive_login(settings, open_browser=not args.no_browser)
    return 0


def cmd_instruments(args, settings: Settings) -> int:
    from .auth import build_kite_client
    from .instruments import InstrumentMaster

    kite = build_kite_client(settings) if args.refresh else None
    master = InstrumentMaster(settings, kite=kite, exchange=args.exchange).load(
        force_refresh=args.refresh
    )
    print(f"{len(master):,} instruments loaded.")
    if args.search:
        hits = master.search(args.search, limit=args.limit)
        if not hits:
            print(f"No match for {args.search!r}.")
            return 1
        print(f"\n{'SYMBOL':<24} {'EXCH':<6} {'TOKEN':>10} {'LOT':>6}  NAME")
        print("-" * 78)
        for i in hits:
            print(f"{i.tradingsymbol:<24} {i.exchange:<6} {i.instrument_token:>10} "
                  f"{i.lot_size:>6}  {i.name[:26]}")
    return 0


def cmd_backtest(args, settings: Settings) -> int:
    from .auth import build_kite_client
    from .backtest import Backtester
    from .data.historical import HistoricalData
    from .strategy import get_strategy

    from .clock import SessionClock

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    kite = build_kite_client(settings)
    instruments = _resolve_instruments(settings, symbols, args.exchange, kite)

    # End at the last completed session by default: today's final candle is
    # still forming, so including it makes repeat runs disagree by a few rupees
    # and quietly poisons any A/B comparison of parameters.
    if args.include_today:
        to_date = date.today()
    else:
        to_date = SessionClock(settings).last_completed_session()
    from_date = to_date - timedelta(days=args.days)
    history = HistoricalData(kite, settings)

    strategy_cls = get_strategy(args.strategy)
    strategy = strategy_cls(instruments, **_parse_params(args.param))
    print(f"\nBacktesting {strategy} on {args.interval} candles, {from_date} .. {to_date}")

    bars = []
    for instrument in instruments:
        fetched = history.fetch(instrument, from_date, to_date, args.interval)
        print(f"  {instrument.tradingsymbol}: {len(fetched):,} candles")
        bars.extend(fetched)

    if not bars:
        print("No candles returned. Historical data needs the paid add-on on your Kite app.")
        return 1

    result = Backtester(strategy, settings, slippage_bps=args.slippage).run(bars)
    print(result.format_report())

    if args.json:
        print(json.dumps(result.summary(), indent=2))
    if args.trades:
        print(f"\n{'SYMBOL':<14}{'SIDE':<6}{'QTY':>6}  {'ENTRY':>10} {'EXIT':>10} "
              f"{'PNL':>10}  REASON")
        print("-" * 78)
        for t in result.trades:
            print(f"{t.symbol:<14}{t.side:<6}{t.quantity:>6}  {t.entry_price:>10.2f} "
                  f"{t.exit_price:>10.2f} {t.net_pnl:>10.2f}  {t.exit_reason[:22]}")
    return 0


def cmd_run(args, settings: Settings) -> int:
    from .auth import build_kite_client, get_access_token
    from .broker import build_broker
    from .data.stream import TickStream
    from .engine import TradingEngine
    from .strategy import get_strategy

    settings.require_live_ready()
    _banner(settings)

    if settings.is_live and not args.yes:
        print("  This will place REAL orders on your Kite account.")
        if input("  Type LIVE to continue: ").strip() != "LIVE":
            print("  Aborted.")
            return 1

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    kite = build_kite_client(settings)
    instruments = _resolve_instruments(settings, symbols, args.exchange, kite)

    strategy = get_strategy(args.strategy)(instruments, **_parse_params(args.param))
    broker = build_broker(settings, kite=kite)
    engine = TradingEngine(
        strategy, broker, settings,
        instruments=instruments,
        bar_interval_seconds=args.bar_seconds,
    )

    stream = TickStream(settings, get_access_token(settings, allow_interactive=False), instruments,
                        on_order_update=engine._on_order_update)
    engine.run(stream, max_runtime_seconds=args.max_seconds)
    return 0


def cmd_holidays(args, settings: Settings) -> int:
    """Show, seed or derive the NSE trading-holiday calendar."""
    from .holidays import HolidayCalendar

    calendar = HolidayCalendar.load(settings.holiday_file)

    if args.refresh:
        # Derive the real calendar from daily candles: any weekday with no
        # candle for a liquid index was a day the exchange did not trade.
        from .auth import build_kite_client
        from .data.historical import HistoricalData

        kite = build_kite_client(settings)
        instruments = _resolve_instruments(settings, [args.reference], args.exchange, kite)
        to_date = date.today()
        from_date = to_date - timedelta(days=args.days)

        print(f"Deriving holidays from {args.reference} daily candles "
              f"({from_date} .. {to_date}) ...")
        bars = HistoricalData(kite, settings).fetch(
            instruments[0], from_date, to_date, "day"
        )
        if not bars:
            print("No candles returned — cannot derive the calendar. "
                  "Historical data needs the paid add-on on your Kite app.")
            return 1
        derived = HolidayCalendar.derive_from_bars(bars, from_date, min(to_date, bars[-1].timestamp.date()))
        # Name what we can from the fixed-date list; the rest stay generic.
        for year in range(from_date.year, to_date.year + 1):
            from .holidays import fixed_holidays_for
            for when, name in fixed_holidays_for(year).items():
                if derived.is_holiday(when):
                    derived.add(when, name)
        derived.save(settings.holiday_file)
        calendar = derived
        print(f"Derived {len(calendar)} holidays from {len(bars):,} candles.")

    if args.seed:
        years = [int(y) for y in args.seed.split(",")]
        calendar.add_fixed_holidays(*years)
        calendar.save(settings.holiday_file)
        print(f"Seeded fixed-date holidays for {', '.join(map(str, years))}.")

    if args.add:
        for entry in args.add:
            when, _, name = entry.partition("=")
            calendar.add(date.fromisoformat(when.strip()), name.strip() or "holiday")
        calendar.save(settings.holiday_file)
        print(f"Added {len(args.add)} holiday(s).")

    if not len(calendar):
        print(f"\nNo holidays recorded ({settings.holiday_file}).")
        print("Weekends are still skipped. To populate:")
        print("  --refresh          derive the real calendar from Kite candles")
        print("  --seed 2026,2027   add fixed-date national holidays only")
        return 0

    upcoming = [d for d in sorted(calendar.to_dict()) if d >= date.today().isoformat()]
    print(f"\n{len(calendar)} holiday(s) in {settings.holiday_file}")
    print(f"{len(upcoming)} still ahead:\n")
    for day in upcoming[:args.limit]:
        when = date.fromisoformat(day)
        print(f"  {day}  {when:%a}  {calendar.name_for(when)}")
    return 0


def cmd_status(args, settings: Settings) -> int:
    from .store import Store

    _banner(settings)
    store = Store(settings.db_file)
    today = date.today()

    snapshot = store.daily_pnl(today)
    if snapshot:
        print(f"  Today ({today}):")
        for key in ("realised_pnl", "unrealised_pnl", "charges", "trades", "halted", "halt_reason"):
            if snapshot.get(key) not in (None, "", 0) or key in ("realised_pnl", "trades"):
                print(f"    {key:<16}: {snapshot[key]}")
    else:
        print(f"  No activity recorded for {today}.")

    orders = store.orders_for(today)
    if orders:
        print(f"\n  Orders today ({len(orders)}):")
        print(f"    {'SYMBOL':<14}{'SIDE':<6}{'QTY':>6} {'STATUS':<12}{'MODE':<7} REASON")
        for o in orders[-15:]:
            print(f"    {o['tradingsymbol']:<14}{o['side']:<6}{o['quantity']:>6} "
                  f"{o['status']:<12}{o['mode']:<7} {(o['reason'] or '')[:28]}")

    history = store.pnl_history(limit=args.days)
    if len(history) > 1:
        print(f"\n  Last {len(history)} sessions:")
        total = 0.0
        for row in reversed(history):
            net = (row["realised_pnl"] or 0) - (row["charges"] or 0)
            total += net
            print(f"    {row['session_date']}  net {net:>12,.2f}  trades {row['trades']:>3}")
        print(f"    {'TOTAL':<12}    {total:>12,.2f}")

    if args.account:
        from .broker import build_broker
        broker = build_broker(settings)
        margin = broker.margin_available()
        if margin is not None:
            print(f"\n  Available margin: {margin:,.2f}")
        positions = broker.positions()
        if positions:
            print("  Broker positions:")
            for p in positions:
                print(f"    {p.get('tradingsymbol'):<14} qty {p.get('quantity')}")
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kitealgo",
        description="Algorithmic trading on Zerodha Kite Connect. Paper mode by default.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING / ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="authenticate and cache today's access token")
    p.add_argument("--force", action="store_true", help="discard the cached token first")
    p.add_argument("--no-browser", action="store_true", help="don't try to open a browser")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("instruments", help="download / search the instrument master")
    p.add_argument("--refresh", action="store_true", help="re-download from Kite")
    p.add_argument("--exchange", default=None, help="limit to one exchange, e.g. NSE")
    p.add_argument("--search", default=None, help="substring to look for")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_instruments)

    p = sub.add_parser("backtest", help="run a strategy over historical candles")
    p.add_argument("--symbols", required=True, help="comma separated, e.g. INFY,TCS")
    p.add_argument("--exchange", default="NSE")
    p.add_argument("--strategy", default="ema_crossover")
    p.add_argument("--interval", default="5minute")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--slippage", type=float, default=2.0, help="basis points")
    p.add_argument("--param", action="append", default=[], help="strategy param, key=value")
    p.add_argument("--include-today", action="store_true",
                   help="include the in-progress session (makes runs non-reproducible)")
    p.add_argument("--trades", action="store_true", help="list every trade")
    p.add_argument("--json", action="store_true", help="also print metrics as JSON")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("run", help="trade live ticks (paper unless live is confirmed)")
    p.add_argument("--symbols", required=True)
    p.add_argument("--exchange", default="NSE")
    p.add_argument("--strategy", default="ema_crossover")
    p.add_argument("--bar-seconds", type=int, default=300, help="candle size, in seconds")
    p.add_argument("--param", action="append", default=[])
    p.add_argument("--max-seconds", type=float, default=None, help="stop after N seconds")
    p.add_argument("--yes", action="store_true", help="skip the live-mode confirmation prompt")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("holidays", help="show / derive the NSE trading-holiday calendar")
    p.add_argument("--refresh", action="store_true",
                   help="derive the real calendar from Kite daily candles")
    p.add_argument("--reference", default="NIFTY 50",
                   help="instrument whose candles define trading days")
    p.add_argument("--exchange", default="NSE")
    p.add_argument("--days", type=int, default=730, help="how far back to derive")
    p.add_argument("--seed", default=None,
                   help="comma separated years to add fixed-date holidays for")
    p.add_argument("--add", action="append", default=[],
                   help="add one, as YYYY-MM-DD=Name")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_holidays)

    p = sub.add_parser("status", help="show today's orders and PnL")
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--account", action="store_true", help="also query the broker")
    p.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings, args.log_level)
    try:
        return args.func(args, settings)
    except ConfigError as exc:
        print(f"\nConfiguration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:
        log.error("%s: %s", type(exc).__name__, exc, exc_info=args.log_level == "DEBUG")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
