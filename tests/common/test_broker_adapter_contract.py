"""Phase 9: adapter contract tests.

Parametrized over all three real BrokerClient adapters this repo has
(AngelOneBroker, DhanBroker, ICICIBreezeBroker) to prove they satisfy a
common set of BrokerClient-level behaviors identically, regardless of
their very different underlying SDKs/wire formats. This is the "adapter
contract test" Phase 9 asks for -- it does not replace each adapter's own
dedicated test file (test_angelone_broker.py / test_dhan_broker.py /
test_icici_breeze_broker.py), which cover adapter-specific translation
detail these shared tests deliberately do not.

Every adapter is constructed with its own fake SDK double (same doubles
used in the adapter-specific test files) -- no real network call, no real
credentials, anywhere in this file.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trading.common.broker import (
    BrokerClient,
    BrokerConnectionError,
    LiveTradingDisabledError,
    OrderSide,
    OrderType,
    Position,
    Quote,
    ReadOnlyModeError,
)
from trading.common.brokers.angelone import AngelOneBroker
from trading.common.brokers.dhan import DhanBroker
from trading.common.brokers.icici_breeze import ICICIBreezeBroker
from trading.common.config import BrokerCredentials, TradingConfig

SYMBOL = "NIFTY15SEP2623400CE"


class FakeSmartApi:
    def __init__(self, api_key: str):
        self.session_response = {"status": True, "data": {"refreshToken": "rt"}, "message": "SUCCESS"}
        self.ltp_response = {"status": True, "data": {"ltp": 133.6}}
        self.position_response = {"status": True, "data": []}
        self.placeOrder = MagicMock(name="placeOrder")

    def generateSession(self, client_id, password, totp):
        return self.session_response

    def getfeedToken(self):
        return "ft"

    def ltpData(self, exchange, tradingsymbol, symboltoken):
        return self.ltp_response

    def position(self):
        return self.position_response


class FakeDhanApi:
    def __init__(self, client_id: str, access_token: str):
        self.fund_response = {"status": "success", "data": {"availableBalance": 50000.0, "utilizedAmount": 0.0}}
        self.ticker_response = {"status": "success", "data": {"NSE_FNO": {"99999": {"last_price": 133.6}}}}
        self.positions_response = {"status": "success", "data": []}
        self.place_order = MagicMock(name="place_order")

    def get_fund_limits(self):
        return self.fund_response

    def ticker_data(self, securities):
        return self.ticker_response

    def get_positions(self):
        return self.positions_response


class FakeBreezeApi:
    def __init__(self, api_key: str):
        self.quote_response = {"Success": [{"ltp": "133.6"}], "Status": 200, "Error": None}
        self.positions_response = {"Success": [], "Status": 200, "Error": None}
        self.place_order = MagicMock(name="place_order")

    def generate_session(self, api_secret, session_token):
        return {"Success": {}, "Status": 200, "Error": None}

    def get_quotes(self, **kwargs):
        return self.quote_response

    def get_portfolio_positions(self):
        return self.positions_response


def _angel_factory(live: bool, read_only: bool | None):
    fake = FakeSmartApi("ak")
    config = TradingConfig(
        trading_mode="live" if live else "paper",
        credentials=BrokerCredentials(angelone_api_key="ak", angelone_client_id="C1", angelone_password="pw", angelone_totp_secret="JBSWY3DPEHPK3PXP"),
    )
    broker = AngelOneBroker(config, smart_api_factory=lambda k: fake, instrument_resolver=lambda s: ("NFO", "99999"), read_only=read_only)
    return broker, fake.placeOrder


def _dhan_factory(live: bool, read_only: bool | None):
    fake = FakeDhanApi("C123", "tok")
    config = TradingConfig(
        trading_mode="live" if live else "paper",
        credentials=BrokerCredentials(dhan_client_id="C123", dhan_access_token="tok"),
    )
    broker = DhanBroker(config, dhan_client_factory=lambda c, t: fake, instrument_resolver=lambda s: ("NSE_FNO", "99999"), read_only=read_only)
    return broker, fake.place_order


def _icici_factory(live: bool, read_only: bool | None):
    fake = FakeBreezeApi("ak")
    config = TradingConfig(
        trading_mode="live" if live else "paper",
        credentials=BrokerCredentials(icici_breeze_api_key="ak", icici_breeze_api_secret="sec", icici_breeze_session_token="tok"),
    )
    broker = ICICIBreezeBroker(config, breeze_client_factory=lambda k: fake, read_only=read_only)
    return broker, fake.place_order


_ADAPTERS = pytest.mark.parametrize(
    "make_adapter", [_angel_factory, _dhan_factory, _icici_factory],
    ids=["angelone", "dhan", "icici_breeze"],
)


@_ADAPTERS
def test_adapter_implements_broker_client(make_adapter):
    broker, _real_order_mock = make_adapter(live=False, read_only=True)
    assert isinstance(broker, BrokerClient)


@_ADAPTERS
def test_adapter_is_not_connected_before_connect(make_adapter):
    broker, _ = make_adapter(live=False, read_only=True)
    assert broker.is_connected() is False


@_ADAPTERS
def test_adapter_connects_against_its_fake_sdk(make_adapter):
    broker, _ = make_adapter(live=False, read_only=True)
    broker.connect()
    assert broker.is_connected() is True


@_ADAPTERS
def test_adapter_disconnect_clears_connection_state(make_adapter):
    broker, _ = make_adapter(live=False, read_only=True)
    broker.connect()
    broker.disconnect()
    assert broker.is_connected() is False


@_ADAPTERS
def test_adapter_get_quote_returns_a_quote(make_adapter):
    broker, _ = make_adapter(live=False, read_only=True)
    broker.connect()
    quote = broker.get_quote(SYMBOL)
    assert isinstance(quote, Quote)
    assert quote.symbol == SYMBOL
    assert quote.last_price == 133.6


@_ADAPTERS
def test_adapter_get_positions_returns_a_list_of_positions(make_adapter):
    broker, _ = make_adapter(live=False, read_only=True)
    broker.connect()
    positions = broker.get_positions()
    assert isinstance(positions, list)
    assert all(isinstance(p, Position) for p in positions)


@_ADAPTERS
def test_adapter_operations_require_connect_first(make_adapter):
    broker, _ = make_adapter(live=False, read_only=True)
    with pytest.raises(BrokerConnectionError):
        broker.get_quote(SYMBOL)


# -- Safety: every adapter must refuse to place/cancel a real order the SAME way -- #
@_ADAPTERS
def test_adapter_place_order_blocked_in_read_only_mode(make_adapter):
    broker, real_order_mock = make_adapter(live=True, read_only=True)
    broker.connect()
    with pytest.raises(ReadOnlyModeError):
        broker.place_order(SYMBOL, OrderSide.SELL, 65, OrderType.MARKET)
    real_order_mock.assert_not_called()


@_ADAPTERS
def test_adapter_place_order_refuses_when_not_live(make_adapter):
    broker, real_order_mock = make_adapter(live=False, read_only=False)
    broker.connect()
    with pytest.raises(LiveTradingDisabledError):
        broker.place_order(SYMBOL, OrderSide.SELL, 65, OrderType.MARKET)
    real_order_mock.assert_not_called()


@_ADAPTERS
def test_adapter_cancel_order_blocked_in_read_only_mode(make_adapter):
    broker, _ = make_adapter(live=True, read_only=True)
    broker.connect()
    with pytest.raises(ReadOnlyModeError):
        broker.cancel_order("ANY-ID")


@_ADAPTERS
def test_adapter_cancel_order_refuses_when_not_live(make_adapter):
    broker, _ = make_adapter(live=False, read_only=False)
    broker.connect()
    with pytest.raises(LiveTradingDisabledError):
        broker.cancel_order("ANY-ID")


@_ADAPTERS
def test_adapter_repr_never_exposes_secrets(make_adapter):
    broker, _ = make_adapter(live=False, read_only=True)
    broker.connect()
    text = repr(broker)
    for secret in ("pw", "sec", "tok", "JBSWY3DPEHPK3PXP"):
        assert secret not in text
