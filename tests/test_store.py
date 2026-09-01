from datetime import date, datetime

from kitealgo.models import Fill, Order, Side
from kitealgo.store import Store

TODAY = date(2026, 9, 1)
NOW = datetime(2026, 9, 1, 10, 0)


def test_orders_and_fills_round_trip(tmp_path, infy):
    store = Store(tmp_path / "t.db")
    order = Order(infy, Side.BUY, 10)
    order.created_at = NOW
    store.record_order(order, "paper", strategy="ema", reason="cross up", session_date=TODAY)
    store.record_fill(Fill(order.order_id, infy, Side.BUY, 10, 100.0, NOW),
                      "paper", charges=3.0, realised_pnl=0.0, session_date=TODAY)

    orders = store.orders_for(TODAY)
    assert len(orders) == 1
    assert orders[0]["tradingsymbol"] == "INFY" and orders[0]["reason"] == "cross up"

    fills = store.fills_for(TODAY)
    assert len(fills) == 1 and fills[0]["charges"] == 3.0


def test_daily_pnl_upserts_rather_than_duplicating(tmp_path):
    store = Store(tmp_path / "t.db")
    store.update_daily_pnl(TODAY, 500.0, -20.0, 6.0, 1)
    store.update_daily_pnl(TODAY, 700.0, 0.0, 9.0, 2, halted=True, halt_reason="limit")

    snapshot = store.daily_pnl(TODAY)
    assert snapshot["realised_pnl"] == 700.0
    assert snapshot["trades"] == 2
    assert snapshot["halted"] == 1
    assert len(store.pnl_history()) == 1


def test_schema_survives_reopening(tmp_path, infy):
    path = tmp_path / "t.db"
    Store(path).update_daily_pnl(TODAY, 1.0, 0.0, 0.0, 1)
    assert Store(path).daily_pnl(TODAY)["realised_pnl"] == 1.0


def test_missing_day_returns_none(tmp_path):
    assert Store(tmp_path / "t.db").daily_pnl(date(1999, 1, 1)) is None
