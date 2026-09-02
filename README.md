# kitealgo

A safety-first algorithmic trading framework for **Zerodha Kite Connect**.

It gives you the whole path — login, instrument lookup, historical candles, live
websocket ticks, strategies, position sizing, risk limits, order execution,
backtesting and an audit trail — with **paper trading as the default**, so
nothing reaches your real account until you explicitly say so.

```
ticks ──▶ bar aggregator ──▶ strategy ──▶ risk manager ──▶ broker ──▶ SQLite
                                            (sizes, vetoes)   (paper | live)
```

---

## Safety model

Trading code that can lose money should be hard to fire by accident. Four things
stand in the way:

| Guard | What it does |
|---|---|
| **Paper by default** | `KITE_TRADING_MODE` defaults to `paper`. Orders are simulated with slippage and brokerage. |
| **Two-key live mode** | Live needs `KITE_TRADING_MODE=live` **and** `KITE_LIVE_CONFIRM=I_UNDERSTAND_THE_RISK`. Either one alone stays in paper. The CLI also prompts you to type `LIVE`. |
| **Risk manager** | Every entry is sized and vetoed centrally. Strategies emit intent; they cannot size a position or reach the broker directly. |
| **Kill switch** | Once the day's loss crosses `KITE_MAX_DAILY_LOSS_PCT`, open positions are flattened and no new entry is accepted until the next session. |

Secrets never get committed (`.gitignore` covers `.env` and the state directory),
the cached access token is locked to your user account (`0600` on macOS and
Linux; inherited permissions dropped via `icacls` on Windows, since POSIX mode
bits do not exist there), and the logger redacts anything that looks like a key
or token before it reaches a log file.

---

## Setup

```bash
git clone <this repo> && cd Cad_API
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill in your API key and secret
```

Create an app at <https://developers.kite.trade/apps> to get your `api_key` and
`api_secret`. Set the app's redirect URL to match `KITE_REDIRECT_URL`
(`http://127.0.0.1:5000/callback` works out of the box — the login command spins
up a throwaway server on that port to catch the token automatically).

### Log in

```bash
python -m kitealgo.cli login
```

Kite access tokens expire around **6:00 AM IST daily**, so this is a
once-a-morning ritual. The token is cached in `.kitealgo/access_token.json` and
reused until it goes stale.

---

## Usage

```bash
# find an instrument
python -m kitealgo.cli instruments --refresh --search RELIANCE

# backtest before risking anything
python -m kitealgo.cli backtest \
    --symbols INFY,TCS --days 90 --interval 5minute \
    --strategy ema_crossover --param fast_period=9 --param slow_period=21 \
    --trades

# paper-trade live ticks (default mode — no real orders)
python -m kitealgo.cli run --symbols INFY --strategy orb --bar-seconds 300

# build the trading-holiday calendar from Kite's own candles
python -m kitealgo.cli holidays --refresh

# what happened today
python -m kitealgo.cli status --account
```

Backtest output:

```
==========================================================
  BACKTEST RESULT
==========================================================
  Bars processed     :          375
  Trades             :            7  (5W / 2L)
  Win rate           :       71.43%
  ------------------------------------------------------
  Gross PnL          :     1,106.55
  Charges            :       207.08
  Net PnL            :       899.47
  Return on capital  :        0.45%
  ------------------------------------------------------
  Expectancy / trade :       128.50
  Profit factor      :         2.90
  Max drawdown       :       848.86  (0.42%)
==========================================================
```

---

## Trading holidays

The exchange closes for weekends (handled automatically) and ~15 public holidays
a year. Most Indian market holidays follow lunar calendars and move every year,
so **this project does not ship a hardcoded guess at them**. Instead the real
calendar is derived from Kite's own data:

```bash
python -m kitealgo.cli holidays --refresh     # authoritative, self-updating
```

That fetches daily candles for an index and treats any weekday with no candle as
a day the exchange did not trade — which reproduces NSE's list exactly, without
anyone typing dates from memory. The result is cached in
`.kitealgo/holidays.json`.

If you don't have the historical-data add-on:

```bash
python -m kitealgo.cli holidays --seed 2026,2027        # fixed-date national holidays only
python -m kitealgo.cli holidays --add 2026-11-09=Diwali # add movable ones by hand
python -m kitealgo.cli holidays                         # show what's recorded
```

`--seed` adds only holidays on fixed calendar dates (Republic Day, Independence
Day, Gandhi Jayanti, Christmas, Maharashtra Day), skipping any that fall on a
weekend. Everything lunar you must add yourself or get via `--refresh`.

With no calendar at all the engine still skips weekends — it just won't know
about holidays. Verify against
[NSE's official list](https://www.nseindia.com/resources/exchange-communication-holidays)
before relying on this in live trading.

---

## Configuration

All of it lives in `.env` (see `.env.example` for the annotated list).

| Variable | Default | Meaning |
|---|---|---|
| `KITE_TRADING_MODE` | `paper` | `paper` or `live` |
| `KITE_LIVE_CONFIRM` | — | must be `I_UNDERSTAND_THE_RISK` for live |
| `KITE_CAPITAL` | `100000` | notional capital the algo may deploy |
| `KITE_RISK_PER_TRADE` | `0.01` | fraction of capital risked per trade |
| `KITE_MAX_POSITION_PCT` | `0.25` | cap on any single position's notional |
| `KITE_MAX_DAILY_LOSS_PCT` | `0.03` | kill-switch threshold |
| `KITE_MAX_OPEN_POSITIONS` | `5` | simultaneous positions |
| `KITE_MAX_TRADES_PER_DAY` | `20` | entries per session |
| `KITE_TRADE_START_TIME` | `09:20` | entries allowed from |
| `KITE_TRADE_END_TIME` | `15:00` | entries allowed until |
| `KITE_SQUARE_OFF_TIME` | `15:15` | unconditional flatten |
| `KITE_DEFAULT_PRODUCT` | `MIS` | `MIS` / `CNC` / `NRML` |

### How position sizing works

With a stop loss, size is set so that being stopped out costs exactly
`risk_per_trade` of capital:

```
quantity = (capital × risk_per_trade) / |entry − stop|
```

capped by `max_position_pct × capital / entry`, then rounded **down** to a whole
lot. Without a stop, only the notional cap applies — a strategy that doesn't
define its risk gets the conservative treatment, not a bigger position.

---

## Included strategies

Both are **templates that demonstrate the framework**, not vetted edges.
Backtest them on your own instruments before they ever see real capital.

- **`ema_crossover`** — fast/slow EMA cross, ATR-based stop and target.
  Params: `fast_period`, `slow_period`, `atr_period`, `atr_multiple`,
  `reward_multiple`, `allow_short`.
- **`orb`** — opening range breakout. The first `range_minutes` define a box;
  a close beyond it is the entry, the far side of the box is the stop. One trade
  per instrument per day. Params: `range_minutes`, `buffer_pct`,
  `reward_multiple`, `allow_short`.

### Writing your own

```python
from kitealgo.indicators import RSI
from kitealgo.models import Bar, Signal, Side
from kitealgo.strategy.base import Strategy


class RsiReversion(Strategy):
    name = "rsi_rev"

    def _setup(self):
        for instrument in self.instruments:
            self.state.for_token(instrument.instrument_token)["rsi"] = RSI(14)

    @property
    def warmup_bars(self):
        return 15

    def on_bar(self, bar: Bar, position=None):
        state = self.state.for_token(bar.instrument_token)
        rsi = state["rsi"].update(bar.close)
        if rsi is None:
            return []

        instrument = self.instruments[0]
        if position and position.is_open and rsi > 60:
            return [self.exit(position, "RSI back to neutral")]
        if not (position and position.is_open) and rsi < 30:
            return [Signal(instrument, Side.BUY, reason=f"RSI {rsi:.1f} oversold",
                           stop_loss=round(bar.close * 0.99, 2),
                           target=round(bar.close * 1.02, 2))]
        return []
```

Register it in `kitealgo/strategy/__init__.py`'s `REGISTRY` and it becomes
available to `--strategy`. Note what the strategy *doesn't* do: no quantity, no
order placement, no PnL. That separation is what lets the same class run
untouched in a backtest and live.

---

## Backtesting honesty

Two rules are enforced, because breaking either makes results look great and
trade badly:

1. **No look-ahead.** A signal raised on a bar fills at the **next** bar's open,
   never that bar's close.
2. **Pessimistic intrabar fills.** If one candle's range covers both your stop
   and your target, the **stop** is assumed to have hit first. OHLC cannot tell
   you which came first, so the framework takes the unfavourable reading.

Slippage (default 2 bps) and Zerodha's brokerage (0.03% capped at ₹20/order) are
charged on both sides.

---

## Architecture

| Module | Responsibility |
|---|---|
| `config.py` | env-driven settings, risk limits, the live-mode gate |
| `auth.py` | login handshake, token cache with 6 AM expiry |
| `instruments.py` | instrument master, symbol ↔ token lookup, daily cache |
| `models.py` | `Order`, `Fill`, `Position`, `Signal`, `Bar`, `Tick` |
| `broker/` | `PaperBroker` (simulated) and `KiteBroker` (real), same interface |
| `data/` | chunked historical fetch, websocket stream, tick→bar aggregation |
| `indicators.py` | incremental SMA / EMA / RSI / ATR / crossover |
| `strategy/` | `Strategy` base plus the two examples |
| `holidays.py` | trading-holiday calendar, derived from real candles |
| `risk.py` | sizing, caps, kill switch, exit rules |
| `portfolio.py` | positions, mark-to-market, PnL |
| `engine.py` | the live loop |
| `backtest.py` | event-driven backtester + metrics |
| `store.py` | SQLite audit trail of orders, fills, daily PnL |
| `cli.py` | `login` / `instruments` / `backtest` / `run` / `status` |

Nothing outside `broker/kite.py`, `auth.py` and `data/` imports the Kite SDK, so
strategies, risk and backtests are testable without network or credentials.

---

## Tests

```bash
python -m pytest          # 179 tests, no network required
```

Coverage includes position accounting through a flip, every risk gate, RSI
cross-checked against Wilder's reference series, look-ahead bias in the
backtester, stop-before-target resolution, tick-to-bar aggregation with
cumulative-volume deltas, and the live-mode gate.

---

## Before you trade real money

1. Backtest across at least a few hundred trades, including a losing regime.
2. Paper trade a full week — it exercises the websocket, reconnects, square-off
   and the token refresh in ways a backtest cannot.
3. Start live with `KITE_CAPITAL` set to a fraction of what you intend, and
   `KITE_MAX_DAILY_LOSS_PCT` set tight.
4. Watch the first sessions. Reconnects, exchange rejections and holiday
   calendars all show up in live trading and nowhere else.

### Known limits

- **The holiday calendar needs populating once** (`holidays --refresh`, or
  `--seed` plus manual entries). Until then the engine skips weekends only.
- **The live order path has never run against real Kite.** Its safety gating is
  unit-tested, but no order has been placed, synced or rejected against the
  actual API. Paper mode is thoroughly exercised; live is not.
- **Neither example strategy has been validated on real market data** — only on
  synthetic series built to have known properties.
- **Historical data needs the paid add-on** on your Kite Connect app; without it
  `backtest` returns a permission error.
- **Live fills are recorded optimistically.** `KiteBroker.sync_order` polls the
  order book; for high-frequency use, drive fills from the websocket order
  postback instead.
- **Backtests assume your order size doesn't move the market** — fine for liquid
  large caps, not for illiquid names.
- Nothing here is investment advice. You are responsible for every order this
  places.
