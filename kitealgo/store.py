"""SQLite persistence for orders, fills and daily PnL.

Two reasons this exists: an audit trail of what the algo actually did, and
crash recovery — the engine reloads the day's counters on restart so a restart
mid-session does not silently reset the kill switch or the trade count.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Optional

from .models import Fill, Order

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT,
    session_date    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    strategy        TEXT,
    tradingsymbol   TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    order_type      TEXT NOT NULL,
    product         TEXT NOT NULL,
    price           REAL,
    trigger_price   REAL,
    status          TEXT NOT NULL,
    status_message  TEXT,
    mode            TEXT NOT NULL,
    reason          TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT,
    session_date    TEXT NOT NULL,
    filled_at       TEXT NOT NULL,
    tradingsymbol   TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    price           REAL NOT NULL,
    charges         REAL DEFAULT 0,
    realised_pnl    REAL DEFAULT 0,
    mode            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_pnl (
    session_date    TEXT PRIMARY KEY,
    realised_pnl    REAL DEFAULT 0,
    unrealised_pnl  REAL DEFAULT 0,
    charges         REAL DEFAULT 0,
    trades          INTEGER DEFAULT 0,
    halted          INTEGER DEFAULT 0,
    halt_reason     TEXT,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_date);
CREATE INDEX IF NOT EXISTS idx_fills_session ON fills(session_date);
"""


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- writes -----------------------------------------------------------
    def record_order(
        self, order: Order, mode: str, strategy: str = "", reason: str = "",
        session_date: Optional[date] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO orders (order_id, session_date, created_at, strategy,
                   tradingsymbol, exchange, side, quantity, order_type, product,
                   price, trigger_price, status, status_message, mode, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    order.order_id,
                    (session_date or date.today()).isoformat(),
                    (order.created_at or datetime.now()).isoformat(),
                    strategy,
                    order.instrument.tradingsymbol,
                    order.instrument.exchange,
                    order.side.value,
                    order.quantity,
                    order.order_type.value,
                    order.product.value,
                    order.price,
                    order.trigger_price,
                    order.status.value,
                    order.status_message,
                    mode,
                    reason,
                ),
            )

    def record_fill(
        self, fill: Fill, mode: str, charges: float = 0.0, realised_pnl: float = 0.0,
        session_date: Optional[date] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO fills (order_id, session_date, filled_at, tradingsymbol,
                   exchange, side, quantity, price, charges, realised_pnl, mode)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fill.order_id,
                    (session_date or fill.timestamp.date()).isoformat(),
                    fill.timestamp.isoformat(),
                    fill.instrument.tradingsymbol,
                    fill.instrument.exchange,
                    fill.side.value,
                    fill.quantity,
                    fill.price,
                    charges,
                    realised_pnl,
                    mode,
                ),
            )

    def update_daily_pnl(
        self, session_date: date, realised: float, unrealised: float,
        charges: float, trades: int, halted: bool = False, halt_reason: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO daily_pnl (session_date, realised_pnl, unrealised_pnl,
                   charges, trades, halted, halt_reason, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_date) DO UPDATE SET
                     realised_pnl=excluded.realised_pnl,
                     unrealised_pnl=excluded.unrealised_pnl,
                     charges=excluded.charges,
                     trades=excluded.trades,
                     halted=excluded.halted,
                     halt_reason=excluded.halt_reason,
                     updated_at=excluded.updated_at""",
                (
                    session_date.isoformat(), realised, unrealised, charges,
                    trades, int(halted), halt_reason, datetime.now().isoformat(),
                ),
            )

    # -- reads ------------------------------------------------------------
    def orders_for(self, session_date: date) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE session_date=? ORDER BY id", (session_date.isoformat(),)
            ).fetchall()
        return [dict(r) for r in rows]

    def fills_for(self, session_date: date) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM fills WHERE session_date=? ORDER BY id", (session_date.isoformat(),)
            ).fetchall()
        return [dict(r) for r in rows]

    def daily_pnl(self, session_date: date) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM daily_pnl WHERE session_date=?", (session_date.isoformat(),)
            ).fetchone()
        return dict(row) if row else None

    def pnl_history(self, limit: int = 30) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_pnl ORDER BY session_date DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
