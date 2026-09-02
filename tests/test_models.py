from datetime import datetime

import pytest

from kitealgo.models import (
    Fill, Instrument, Order, OrderType, Position, Side,
)

NOW = datetime(2026, 9, 1, 10, 0)


def test_side_helpers():
    assert Side.BUY.opposite is Side.SELL
    assert Side.BUY.sign == 1 and Side.SELL.sign == -1


def test_tick_rounding(infy):
    assert infy.round_to_tick(100.123) == 100.10
    assert infy.round_to_tick(100.17) == 100.15
    assert infy.key == "NSE:INFY"


def test_order_validates_its_inputs(infy):
    with pytest.raises(ValueError, match="quantity"):
        Order(infy, Side.BUY, 0)
    with pytest.raises(ValueError, match="requires a price"):
        Order(infy, Side.BUY, 1, OrderType.LIMIT)
    with pytest.raises(ValueError, match="trigger_price"):
        Order(infy, Side.SELL, 1, OrderType.SLM)


def test_order_tag_truncated_to_kite_limit(infy):
    order = Order(infy, Side.BUY, 1, tag="a-very-long-strategy-name")
    assert len(order.tag) == 20


def test_kite_params_round_prices_to_tick(infy):
    order = Order(infy, Side.BUY, 5, OrderType.LIMIT, price=100.123, tag="ema")
    params = order.to_kite_params()
    assert params["price"] == 100.10
    assert params["transaction_type"] == "BUY"
    assert params["tradingsymbol"] == "INFY"
    assert params["variety"] == "regular"


def test_position_averages_up(infy):
    position = Position(infy)
    position.apply_fill(Fill("1", infy, Side.BUY, 10, 100.0, NOW))
    position.apply_fill(Fill("2", infy, Side.BUY, 10, 110.0, NOW))
    assert position.quantity == 20
    assert position.average_price == 105.0


def test_position_books_pnl_on_partial_close(infy):
    position = Position(infy)
    position.apply_fill(Fill("1", infy, Side.BUY, 20, 105.0, NOW))
    realised = position.apply_fill(Fill("2", infy, Side.SELL, 5, 120.0, NOW))
    assert realised == pytest.approx(75.0)
    assert position.quantity == 15
    assert position.average_price == 105.0   # unchanged by a partial exit


def test_position_flip_books_only_the_closed_part(infy):
    position = Position(infy)
    position.apply_fill(Fill("1", infy, Side.BUY, 15, 105.0, NOW))
    realised = position.apply_fill(Fill("2", infy, Side.SELL, 25, 90.0, NOW))
    assert realised == pytest.approx(-225.0)      # 15 shares closed at -15 each
    assert position.quantity == -10               # remainder opens short
    assert position.average_price == 90.0


def test_flat_position_clears_protective_levels(infy):
    position = Position(infy, stop_loss=95.0, target=115.0)
    position.apply_fill(Fill("1", infy, Side.BUY, 10, 100.0, NOW))
    position.stop_loss, position.target = 95.0, 115.0
    position.apply_fill(Fill("2", infy, Side.SELL, 10, 108.0, NOW))
    assert not position.is_open
    assert position.stop_loss is None and position.target is None


def test_unrealised_pnl_tracks_last_price(infy):
    position = Position(infy)
    position.apply_fill(Fill("1", infy, Side.BUY, 10, 100.0, NOW))
    position.last_price = 105.0
    assert position.unrealised_pnl == pytest.approx(50.0)
    assert position.exposure == pytest.approx(1050.0)


def test_short_position_pnl(infy):
    position = Position(infy)
    position.apply_fill(Fill("1", infy, Side.SELL, 10, 100.0, NOW))
    position.last_price = 95.0
    assert position.is_short
    assert position.unrealised_pnl == pytest.approx(50.0)
