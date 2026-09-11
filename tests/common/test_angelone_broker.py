"""trading/common/brokers/angelone.py -- AngelOneBroker exercised entirely
against a FakeSmartApi double. No real network calls, no real credentials,
no SmartApi/pyotp SDK behavior relied upon beyond pyotp's pure-local TOTP
math (no network I/O). smart_api_factory/instrument_resolver injection
means the real `from SmartApi import SmartConnect` import is never reached.
"""
from __future__ import annotations

import pytest

from trading.common.broker import (
    BrokerAuthenticationError,
    BrokerConfigError,
    BrokerConnectionError,
    BrokerRateLimitError,
    LiveTradingDisabledError,
    OrderSide,
    OrderType,
)
from trading.common.brokers.angelone import AccountInfo, AngelOneBroker, FundsSnapshot
from trading.common.config import BrokerCredentials, TradingConfig
from trading.common.execution import OrderState


class FakeSmartApi:
    """Stands in for SmartApi.SmartConnect. Every method mirrors the shape
    the adapter actually calls; behavior is configured per-test."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session_response: dict = {
            "status": True,
            "data": {"refreshToken": "fake-refresh-token"},
            "message": "SUCCESS",
        }
        self.feed_token = "fake-feed-token"
        self.orders: dict[str, dict] = {}
        self.place_order_result: str | None = "AO-1"
        self.place_order_exception: Exception | None = None
        self.cancel_result = True
        self.modify_result = True
        self.ltp_response: dict = {"status": True, "data": {"ltp": 125.5}}
        self.position_response: dict = {"status": True, "data": []}
        self.profile_response: dict = {
            "status": True,
            "data": {"clientcode": "C123", "name": "Test User", "email": "t@example.com"},
        }
        self.funds_response: dict = {"status": True, "data": {"availablecash": 50000.0, "utiliseddebits": 1000.0}}
        self.placed_params: list[dict] = []
        self.modified_params: list[dict] = []

    def generateSession(self, client_id, password, totp):
        return self.session_response

    def getfeedToken(self):
        return self.feed_token

    def placeOrder(self, params):
        self.placed_params.append(params)
        if self.place_order_exception:
            raise self.place_order_exception
        return self.place_order_result

    def cancelOrder(self, order_id, variety):
        return self.cancel_result

    def modifyOrder(self, params):
        self.modified_params.append(params)
        return self.modify_result

    def orderBook(self):
        return {"status": True, "data": list(self.orders.values())}

    def position(self):
        return self.position_response

    def ltpData(self, exchange, tradingsymbol, symboltoken):
        return self.ltp_response

    def getProfile(self, refresh_token):
        return self.profile_response

    def rmsLimit(self):
        return self.funds_response


def _config(live: bool = False) -> TradingConfig:
    return TradingConfig(
        trading_mode="live" if live else "paper",
        credentials=BrokerCredentials(
            angelone_api_key="ak",
            angelone_client_id="C123",
            angelone_password="pw",
            angelone_totp_secret="JBSWY3DPEHPK3PXP",  # a syntactically valid base32 TOTP seed, not a real secret
        ),
    )


def _broker(config: TradingConfig | None = None, fake: FakeSmartApi | None = None):
    fake = fake or FakeSmartApi("ak")
    broker = AngelOneBroker(
        config or _config(),
        smart_api_factory=lambda api_key: fake,
        instrument_resolver=lambda symbol: ("NFO", "99999"),
    )
    return broker, fake


# -- Authentication ----------------------------------------------------------- #
def test_connect_succeeds_with_mocked_smart_connect():
    broker, fake = _broker()
    broker.connect()
    assert broker.is_connected() is True


def test_connect_fails_when_session_response_reports_failure():
    fake = FakeSmartApi("ak")
    fake.session_response = {"status": False, "message": "Invalid TOTP"}
    broker, _ = _broker(fake=fake)

    with pytest.raises(BrokerAuthenticationError):
        broker.connect()
    assert broker.is_connected() is False


def test_connect_fails_when_credentials_missing():
    config = TradingConfig(credentials=BrokerCredentials())  # nothing set
    broker, _ = _broker(config=config)

    with pytest.raises(BrokerConfigError):
        broker.connect()


def test_connect_classifies_a_raw_sdk_exception_during_login():
    fake = FakeSmartApi("ak")

    def _raise(*_args, **_kwargs):
        raise RuntimeError("Invalid password provided")

    fake.generateSession = _raise
    broker, _ = _broker(fake=fake)

    with pytest.raises(BrokerAuthenticationError):
        broker.connect()


# -- Connection lifecycle ------------------------------------------------------- #
def test_disconnect_clears_connection_state():
    broker, _ = _broker()
    broker.connect()
    broker.disconnect()
    assert broker.is_connected() is False


def test_operations_require_connect_first():
    broker, _ = _broker()
    with pytest.raises(BrokerConnectionError):
        broker.get_quote("NIFTY30JUL2625000CE")


# -- Account / funds ------------------------------------------------------------ #
def test_get_account_info_normalization():
    broker, _ = _broker()
    broker.connect()

    info = broker.get_account_info()

    assert info == AccountInfo(client_id="C123", name="Test User", email="t@example.com")


def test_get_funds_normalization():
    broker, _ = _broker()
    broker.connect()

    funds = broker.get_funds()

    assert funds == FundsSnapshot(available_cash=50000.0, used_margin=1000.0)


# -- Market data ----------------------------------------------------------------- #
def test_get_quote_normalization():
    broker, _ = _broker()
    broker.connect()

    quote = broker.get_quote("NIFTY30JUL2625000CE")

    assert quote.symbol == "NIFTY30JUL2625000CE"
    assert quote.last_price == 125.5
    assert quote.timestamp  # non-empty ISO string


def test_get_quote_raises_generic_error_on_malformed_response():
    fake = FakeSmartApi("ak")
    fake.ltp_response = {"status": True, "data": {"unexpected": "shape"}}
    broker, _ = _broker(fake=fake)
    broker.connect()

    with pytest.raises(BrokerConnectionError):
        broker.get_quote("NIFTY30JUL2625000CE")


def test_get_quote_converts_a_broker_reported_failure():
    fake = FakeSmartApi("ak")
    fake.ltp_response = {"status": False, "message": "symbol not found"}
    broker, _ = _broker(fake=fake)
    broker.connect()

    with pytest.raises(BrokerConnectionError):
        broker.get_quote("NIFTY30JUL2625000CE")


# -- Orders: translation -------------------------------------------------------- #
def test_market_order_translation():
    broker, fake = _broker(config=_config(live=True))
    broker.connect()

    result = broker.place_order("NIFTY30JUL2625000CE", OrderSide.BUY, 50, OrderType.MARKET)

    params = fake.placed_params[-1]
    assert params["transactiontype"] == "BUY"
    assert params["ordertype"] == "MARKET"
    assert params["quantity"] == "50"
    assert params["price"] == "0"
    assert params["exchange"] == "NFO"
    assert params["symboltoken"] == "99999"
    assert result.status == "OPEN"
    assert result.order_id == "AO-1"


def test_limit_order_translation_with_price_and_sell_side():
    broker, fake = _broker(config=_config(live=True))
    broker.connect()

    broker.place_order("NIFTY30JUL2625000PE", OrderSide.SELL, 50, OrderType.LIMIT, limit_price=132.35)

    params = fake.placed_params[-1]
    assert params["transactiontype"] == "SELL"
    assert params["ordertype"] == "LIMIT"
    assert params["price"] == "132.35"
    assert params["variety"] == "NORMAL"
    assert params["producttype"] == "INTRADAY"


def test_limit_order_without_price_raises():
    broker, _ = _broker(config=_config(live=True))
    broker.connect()

    with pytest.raises(ValueError):
        broker.place_order("NIFTY30JUL2625000CE", OrderSide.BUY, 50, OrderType.LIMIT, limit_price=None)


def test_place_order_refuses_when_not_live():
    broker, _ = _broker(config=_config(live=False))
    broker.connect()

    with pytest.raises(LiveTradingDisabledError):
        broker.place_order("NIFTY30JUL2625000CE", OrderSide.BUY, 50, OrderType.MARKET)


def test_stop_orders_are_not_yet_representable():
    """Documents a known boundary: BrokerClient.place_order() has no STOP
    OrderType and no trigger_price parameter, so Angel's STOPLOSS_LIMIT
    order type (used live only by Vwap_Algo_Nifty_hedge, untouched here)
    cannot be requested through this adapter yet."""
    assert not hasattr(OrderType, "STOP")
    import inspect

    assert "trigger_price" not in inspect.signature(AngelOneBroker.place_order).parameters


def test_rejected_order_returns_a_rejected_result_not_an_exception():
    fake = FakeSmartApi("ak")
    fake.place_order_result = None
    broker, _ = _broker(config=_config(live=True), fake=fake)
    broker.connect()

    result = broker.place_order("NIFTY30JUL2625000CE", OrderSide.BUY, 50, OrderType.MARKET)

    assert result.status == "REJECTED"
    assert result.order_id == ""


# -- Order lifecycle -------------------------------------------------------------- #
def test_get_order_maps_complete_status():
    fake = FakeSmartApi("ak")
    fake.orders["AO-1"] = {"orderid": "AO-1", "status": "complete", "quantity": "50", "filledshares": "50"}
    broker, _ = _broker(fake=fake)
    broker.connect()

    state = broker.get_order("AO-1")

    assert state == OrderState(order_id="AO-1", status="COMPLETE", filled_quantity=50, remaining_quantity=0)


def test_get_order_maps_open_and_trigger_pending_to_open():
    fake = FakeSmartApi("ak")
    fake.orders["AO-1"] = {"orderid": "AO-1", "status": "trigger pending", "quantity": "50", "filledshares": "0"}
    broker, _ = _broker(fake=fake)
    broker.connect()

    state = broker.get_order("AO-1")

    assert state.status == "OPEN"
    assert state.remaining_quantity == 50


def test_get_order_reports_partial_fill():
    fake = FakeSmartApi("ak")
    fake.orders["AO-1"] = {"orderid": "AO-1", "status": "open", "quantity": "50", "filledshares": "20"}
    broker, _ = _broker(fake=fake)
    broker.connect()

    state = broker.get_order("AO-1")

    assert state.filled_quantity == 20
    assert state.remaining_quantity == 30


def test_get_order_unknown_id_returns_unknown_status():
    broker, _ = _broker()
    broker.connect()

    state = broker.get_order("NO-SUCH-ID")

    assert state.status == "UNKNOWN"


def test_modify_order_reuses_order_book_row_for_identifying_fields():
    fake = FakeSmartApi("ak")
    fake.orders["AO-1"] = {
        "orderid": "AO-1", "status": "open", "quantity": "50", "filledshares": "0",
        "tradingsymbol": "NIFTY30JUL2625000CE", "symboltoken": "99999", "exchange": "NFO",
        "producttype": "INTRADAY",
    }
    broker, _ = _broker(fake=fake)
    broker.connect()

    ok = broker.modify_order("AO-1", 30, 128.0)

    assert ok is True
    params = fake.modified_params[-1]
    assert params["tradingsymbol"] == "NIFTY30JUL2625000CE"
    assert params["price"] == "128.0"
    assert params["quantity"] == "30"


def test_modify_order_unknown_id_raises():
    broker, _ = _broker()
    broker.connect()

    with pytest.raises(BrokerConnectionError):
        broker.modify_order("NO-SUCH-ID", 10, 100.0)


def test_cancel_order():
    broker, fake = _broker(config=_config(live=True))
    broker.connect()

    assert broker.cancel_order("AO-1") is True


def test_cancel_order_refuses_when_not_live():
    broker, _ = _broker(config=_config(live=False))
    broker.connect()

    with pytest.raises(LiveTradingDisabledError):
        broker.cancel_order("AO-1")


# -- Positions ------------------------------------------------------------------- #
def test_get_positions_normalization():
    fake = FakeSmartApi("ak")
    fake.position_response = {
        "status": True,
        "data": [
            {
                "tradingsymbol": "NIFTY30JUL2625000CE", "netqty": "-50", "avgnetprice": "120.5",
                "ltp": "118.0", "realised": "0", "unrealised": "125.0", "pnl": "125.0",
            }
        ],
    }
    broker, _ = _broker(fake=fake)
    broker.connect()

    positions = broker.get_positions()

    assert len(positions) == 1
    assert positions[0].symbol == "NIFTY30JUL2625000CE"
    assert positions[0].quantity == -50
    assert positions[0].pnl == 125.0


def test_get_positions_skips_malformed_rows_without_failing():
    fake = FakeSmartApi("ak")
    fake.position_response = {"status": True, "data": [{"missing": "tradingsymbol"}]}
    broker, _ = _broker(fake=fake)
    broker.connect()

    assert broker.get_positions() == []


def test_get_positions_allows_empty_data():
    fake = FakeSmartApi("ak")
    fake.position_response = {"status": True, "data": None}
    broker, _ = _broker(fake=fake)
    broker.connect()

    assert broker.get_positions() == []


# -- Errors ------------------------------------------------------------------------ #
def test_rate_limit_error_is_classified():
    fake = FakeSmartApi("ak")
    fake.place_order_exception = RuntimeError("Access denied because of exceeding access rate")
    broker, _ = _broker(config=_config(live=True), fake=fake)
    broker.connect()

    with pytest.raises(BrokerRateLimitError):
        broker.place_order("NIFTY30JUL2625000CE", OrderSide.BUY, 50, OrderType.MARKET)


def test_generic_connection_error_for_unrecognized_sdk_exception():
    fake = FakeSmartApi("ak")
    fake.place_order_exception = RuntimeError("some transient network blip")
    broker, _ = _broker(config=_config(live=True), fake=fake)
    broker.connect()

    with pytest.raises(BrokerConnectionError):
        broker.place_order("NIFTY30JUL2625000CE", OrderSide.BUY, 50, OrderType.MARKET)


def test_broker_rate_limit_error_is_a_connection_error_subclass():
    assert issubclass(BrokerRateLimitError, BrokerConnectionError)


# -- Security: no secrets leak ------------------------------------------------------ #
SECRET_VALUES = ("pw", "JBSWY3DPEHPK3PXP", "fake-feed-token", "fake-refresh-token")


def test_repr_never_exposes_secrets():
    broker, _ = _broker()
    broker.connect()

    text = repr(broker)

    for secret in SECRET_VALUES:
        assert secret not in text


def test_exception_messages_never_include_credentials():
    fake = FakeSmartApi("ak")
    fake.session_response = {"status": False, "message": "Invalid TOTP"}
    broker, _ = _broker(fake=fake)

    with pytest.raises(BrokerAuthenticationError) as excinfo:
        broker.connect()

    text = str(excinfo.value)
    for secret in ("pw", "JBSWY3DPEHPK3PXP"):
        assert secret not in text


def test_config_error_message_never_includes_partial_credentials():
    config = TradingConfig(credentials=BrokerCredentials(angelone_client_id="C123"))
    broker, _ = _broker(config=config)

    with pytest.raises(BrokerConfigError) as excinfo:
        broker.connect()

    assert "C123" not in str(excinfo.value)
