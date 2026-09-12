"""trading/common/brokers/connected_shadow_broker.py -- Phase 5B.

The "real" broker here is a real AngelOneBroker wired to a FakeSmartApi
double (same pattern as tests/common/test_angelone_broker.py) -- no real
network call anywhere in this file. The point of these tests is to prove
the DELEGATION boundary: reads go to the real broker, writes go to
ShadowBroker, and no configuration or call sequence can make a write reach
the real broker's underlying (fake) SmartAPI mutation methods.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trading.common.broker import OrderSide, OrderType
from trading.common.brokers.angelone import AngelOneBroker
from trading.common.brokers.connected_shadow_broker import ConnectedShadowBroker
from trading.common.brokers.shadow_broker import ShadowBroker
from trading.common.config import BrokerCredentials, TradingConfig
from trading.common.trading_account import ExecutionMode


class FakeSmartApi:
    def __init__(self, api_key: str):
        self.session_response = {"status": True, "data": {"refreshToken": "rt"}, "message": "SUCCESS"}
        self.feed_token = "ft"
        self.ltp_response = {"status": True, "data": {"ltp": 24800.5}}
        self.position_response = {"status": True, "data": [{"tradingsymbol": "REAL-POSITION", "netqty": "10", "avgnetprice": "1", "ltp": "1", "pnl": "0"}]}
        self.orderbook_response = {"status": True, "data": []}
        self.funds_response = {"status": True, "data": {"availablecash": 1000.0, "utiliseddebits": 0.0}}
        self.profile_response = {"status": True, "data": {"clientcode": "C1", "name": "Real Account", "email": "t@example.com"}}
        self.placeOrder = MagicMock(name="placeOrder")
        self.modifyOrder = MagicMock(name="modifyOrder")
        self.cancelOrder = MagicMock(name="cancelOrder")

    def generateSession(self, client_id, password, totp):
        return self.session_response

    def getfeedToken(self):
        return self.feed_token

    def ltpData(self, exchange, tradingsymbol, symboltoken):
        return self.ltp_response

    def position(self):
        return self.position_response

    def orderBook(self):
        return self.orderbook_response

    def rmsLimit(self):
        return self.funds_response

    def getProfile(self, refresh_token):
        return self.profile_response


def _real_broker(read_only=True, fake=None):
    fake = fake or FakeSmartApi("ak")
    config = TradingConfig(
        trading_mode="paper",
        credentials=BrokerCredentials(
            angelone_api_key="ak", angelone_client_id="C1", angelone_password="pw",
            angelone_totp_secret="JBSWY3DPEHPK3PXP",
        ),
    )
    broker = AngelOneBroker(
        config, smart_api_factory=lambda k: fake,
        instrument_resolver=lambda symbol: ("NFO", "99999"), read_only=read_only,
    )
    return broker, fake


def _connected():
    real, fake = _real_broker()
    real.connect()
    connected = ConnectedShadowBroker(real, ShadowBroker(), execution_mode=ExecutionMode.SHADOW)
    connected.connect()
    return connected, real, fake


# --------------------------------------------------------------------------- #
# Fail-closed construction
# --------------------------------------------------------------------------- #
def test_construction_requires_execution_mode_explicitly():
    real, _fake = _real_broker()
    with pytest.raises(ValueError, match="explicitly"):
        ConnectedShadowBroker(real, ShadowBroker())  # execution_mode omitted


def test_construction_rejects_live_execution_mode():
    real, _fake = _real_broker()
    with pytest.raises(ValueError, match="SHADOW"):
        ConnectedShadowBroker(real, ShadowBroker(), execution_mode=ExecutionMode.LIVE)


def test_construction_rejects_paper_execution_mode():
    real, _fake = _real_broker()
    with pytest.raises(ValueError, match="SHADOW"):
        ConnectedShadowBroker(real, ShadowBroker(), execution_mode=ExecutionMode.PAPER)


def test_construction_accepts_plain_string_shadow():
    real, _fake = _real_broker()
    connected = ConnectedShadowBroker(real, ShadowBroker(), execution_mode="SHADOW")
    assert connected.execution_mode == ExecutionMode.SHADOW


def test_construction_rejects_a_real_broker_that_is_not_read_only():
    real, _fake = _real_broker(read_only=False)
    with pytest.raises(ValueError, match="read_only"):
        ConnectedShadowBroker(real, ShadowBroker(), execution_mode=ExecutionMode.SHADOW)


# --------------------------------------------------------------------------- #
# Real reads
# --------------------------------------------------------------------------- #
def test_get_quote_delegates_to_the_real_broker():
    connected, _real, _fake = _connected()
    quote = connected.get_quote("NIFTY19MAY2623700CE")
    assert quote.last_price == 24800.5


def test_resolve_instrument_delegates_to_the_real_broker():
    connected, _real, _fake = _connected()
    exchange, token = connected.resolve_instrument("NIFTY19MAY2623700CE")
    assert exchange == "NFO"
    assert token == "99999"


def test_get_funds_delegates_to_the_real_broker():
    connected, _real, _fake = _connected()
    funds = connected.get_funds()
    assert funds.available_cash == 1000.0


def test_get_account_info_delegates_to_the_real_broker():
    connected, _real, _fake = _connected()
    info = connected.get_account_info()
    assert info.client_id == "C1"


def test_connect_reaches_the_real_broker():
    connected, real, _fake = _connected()
    assert real.is_connected() is True
    assert connected.is_connected() is True


# --------------------------------------------------------------------------- #
# Simulated writes -- CRITICAL safety tests
# --------------------------------------------------------------------------- #
def test_place_order_never_calls_the_real_smart_api():
    connected, _real, fake = _connected()

    result = connected.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)

    assert result.status == "COMPLETE"
    assert result.order_id.startswith("SHADOW-")
    fake.placeOrder.assert_not_called()


def test_modify_order_never_calls_the_real_smart_api():
    connected, _real, fake = _connected()
    connected.configure_next_order("NIFTY19MAY2623700CE", "PARTIAL", fill_ratio=0.5)
    result = connected.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 60, OrderType.LIMIT, limit_price=125.5)

    connected.modify_order(result.order_id, 30, 130.0)

    fake.modifyOrder.assert_not_called()


def test_cancel_order_never_calls_the_real_smart_api():
    connected, _real, fake = _connected()
    connected.configure_next_order("NIFTY19MAY2623700CE", "OPEN")
    result = connected.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)

    connected.cancel_order(result.order_id)

    fake.cancelOrder.assert_not_called()


def test_many_simulated_orders_never_touch_the_real_smart_api():
    connected, _real, fake = _connected()

    for _ in range(10):
        connected.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 5, OrderType.MARKET)

    fake.placeOrder.assert_not_called()
    fake.modifyOrder.assert_not_called()
    fake.cancelOrder.assert_not_called()


def test_place_order_body_never_references_the_real_broker():
    """Structural guard, independent of the runtime assert: inspect the
    source of the three mutating methods and confirm self._real_broker
    never appears in them."""
    import inspect

    from trading.common.brokers.connected_shadow_broker import ConnectedShadowBroker as CSB

    for method_name in ("place_order", "modify_order", "cancel_order"):
        source = inspect.getsource(getattr(CSB, method_name))
        assert "_real_broker" not in source, f"{method_name} references the real broker!"


# --------------------------------------------------------------------------- #
# Positions/orders are ALWAYS simulated, never the real account's
# --------------------------------------------------------------------------- #
def test_get_positions_returns_simulated_positions_not_the_real_accounts():
    connected, real, _fake = _connected()

    # The real (fake-backed) broker DOES have a real position on file...
    real_positions = real.get_positions()
    assert any(p.symbol == "REAL-POSITION" for p in real_positions)

    # ...but ConnectedShadowBroker.get_positions() must never surface it.
    assert connected.get_positions() == []

    connected.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)
    simulated_positions = connected.get_positions()
    assert all(p.symbol != "REAL-POSITION" for p in simulated_positions)
    assert any(p.symbol == "NIFTY19MAY2623700CE" for p in simulated_positions)


def test_get_order_book_returns_simulated_orders_only():
    connected, _real, _fake = _connected()
    connected.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)

    book = connected.get_order_book()

    assert len(book) == 1
    assert book[0].order_id.startswith("SHADOW-")
