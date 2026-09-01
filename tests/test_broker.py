from datetime import datetime

import pytest

from kitealgo.broker.base import BrokerError
from kitealgo.broker.paper import PaperBroker
from kitealgo.config import IST, ConfigError, Settings
from kitealgo.models import Order, OrderStatus, OrderType, Side

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=IST)


@pytest.fixture
def broker(infy):
    broker = PaperBroker(slippage_bps=0.0)
    broker.set_clock(NOW)
    broker.set_price(infy, 100.0)
    return broker


def test_market_order_fills_immediately(broker, infy):
    order = broker.place_order(Order(infy, Side.BUY, 10))
    assert order.status is OrderStatus.COMPLETE
    assert order.average_price == 100.0
    assert len(broker.fills()) == 1


def test_slippage_moves_against_you(infy):
    broker = PaperBroker(slippage_bps=100.0)   # 1%
    broker.set_price(infy, 100.0)
    buy = broker.place_order(Order(infy, Side.BUY, 1))
    broker.set_price(infy, 100.0)
    sell = broker.place_order(Order(infy, Side.SELL, 1))
    assert buy.average_price > 100.0
    assert sell.average_price < 100.0


def test_unmarketable_limit_rests_then_fills(broker, infy):
    order = broker.place_order(Order(infy, Side.BUY, 10, OrderType.LIMIT, price=95.0))
    assert order.status is OrderStatus.OPEN

    assert broker.on_price(infy, 96.0) == []          # not yet
    fills = broker.on_price(infy, 94.0)
    assert len(fills) == 1
    assert order.status is OrderStatus.COMPLETE
    assert order.average_price == 95.0                # fills at the limit, not the market


def test_marketable_limit_fills_at_once(broker, infy):
    order = broker.place_order(Order(infy, Side.BUY, 10, OrderType.LIMIT, price=105.0))
    assert order.status is OrderStatus.COMPLETE


def test_stop_loss_sell_triggers_on_the_way_down(broker, infy):
    order = broker.place_order(Order(infy, Side.SELL, 10, OrderType.SLM, trigger_price=95.0))
    assert order.status is OrderStatus.OPEN
    assert broker.on_price(infy, 96.0) == []
    assert len(broker.on_price(infy, 94.0)) == 1
    assert order.status is OrderStatus.COMPLETE


def test_stop_loss_buy_triggers_on_the_way_up(broker, infy):
    order = broker.place_order(Order(infy, Side.BUY, 10, OrderType.SLM, trigger_price=105.0))
    assert broker.on_price(infy, 104.0) == []
    assert len(broker.on_price(infy, 106.0)) == 1


def test_order_without_a_price_is_rejected(infy):
    broker = PaperBroker()
    with pytest.raises(BrokerError, match="no price"):
        broker.place_order(Order(infy, Side.BUY, 1))


def test_cancel_removes_resting_order(broker, infy):
    order = broker.place_order(Order(infy, Side.SELL, 10, OrderType.SLM, trigger_price=95.0))
    broker.cancel_order(order)
    assert order.status is OrderStatus.CANCELLED
    assert broker.on_price(infy, 90.0) == []


def test_brokerage_is_capped(broker, infy):
    assert broker.charges_for(10_000) == pytest.approx(3.0)      # 0.03%
    assert broker.charges_for(10_000_000) == 20.0                # capped


def test_positions_net_out(broker, infy):
    broker.place_order(Order(infy, Side.BUY, 10))
    broker.place_order(Order(infy, Side.SELL, 4))
    assert broker.positions()[0]["quantity"] == 6


def test_live_broker_refuses_unconfirmed_settings(monkeypatch):
    from kitealgo.broker.kite import KiteBroker
    monkeypatch.setenv("KITE_TRADING_MODE", "live")
    monkeypatch.delenv("KITE_LIVE_CONFIRM", raising=False)
    with pytest.raises(ConfigError, match="Refusing to build a live broker"):
        KiteBroker(object(), Settings.from_env())


def test_factory_returns_paper_by_default(settings):
    from kitealgo.broker import build_broker
    assert isinstance(build_broker(settings), PaperBroker)


def test_factory_blocks_unconfirmed_live(monkeypatch):
    from kitealgo.broker import build_broker
    monkeypatch.setenv("KITE_TRADING_MODE", "live")
    monkeypatch.delenv("KITE_LIVE_CONFIRM", raising=False)
    with pytest.raises(ConfigError):
        build_broker(Settings.from_env())
