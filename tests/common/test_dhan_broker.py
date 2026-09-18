"""trading/common/brokers/dhan.py -- DhanBroker exercised entirely against
a FakeDhanApi double. No real network call, no real credentials -- same
mocking discipline as tests/common/test_angelone_broker.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trading.common.broker import (
    BrokerAuthenticationError,
    BrokerConfigError,
    BrokerConnectionError,
    BrokerRateLimitError,
    LiveTradingDisabledError,
    OrderSide,
    OrderType,
    ReadOnlyModeError,
)
from trading.common.brokers.dhan import AccountInfo, DhanBroker, FundsSnapshot
from trading.common.config import BrokerCredentials, TradingConfig
from trading.common.execution import OrderState


class FakeDhanApi:
    """Mirrors dhanhq's client surface -- no network, no real SDK object."""

    def __init__(self, client_id: str, access_token: str):
        self.client_id = client_id
        self.fund_response = {
            "status": "success",
            "data": {"availableBalance": 50000.0, "utilizedAmount": 1000.0, "dhanClientId": client_id, "name": "Test User", "email": "t@example.com"},
        }
        self.positions_response = {"status": "success", "data": []}
        self.orders: dict[str, dict] = {}
        self.place_order_response = {"orderId": "DH-1", "orderStatus": "PENDING"}
        self.ticker_response = {"status": "success", "data": {"NSE_FNO": {"99999": {"last_price": 133.6}}}}
        self.place_order = MagicMock(name="place_order", side_effect=lambda **k: self.place_order_response)
        self.cancel_order = MagicMock(name="cancel_order", return_value={"status": "success"})

    def get_fund_limits(self):
        return self.fund_response

    def get_positions(self):
        return self.positions_response

    def get_order_list(self):
        return {"status": "success", "data": list(self.orders.values())}

    def ticker_data(self, securities):
        return self.ticker_response

    def modify_order(self, order_id, order_type, quantity, price, validity):
        return {"status": "success"}


def _config(live: bool = False) -> TradingConfig:
    return TradingConfig(
        trading_mode="live" if live else "paper",
        credentials=BrokerCredentials(dhan_client_id="C123", dhan_access_token="tok-abc"),
    )


def _broker(config: TradingConfig | None = None, fake: FakeDhanApi | None = None, read_only: bool | None = False):
    fake = fake or FakeDhanApi("C123", "tok-abc")
    broker = DhanBroker(
        config or _config(),
        dhan_client_factory=lambda cid, tok: fake,
        instrument_resolver=lambda symbol: ("NSE_FNO", "99999"),
        read_only=read_only,
    )
    return broker, fake


SYMBOL = "NIFTY15SEP2623400CE"


# -- Authentication ----------------------------------------------------------- #
def test_connect_succeeds_with_mocked_dhan_client():
    broker, _fake = _broker()
    broker.connect()
    assert broker.is_connected() is True


def test_connect_fails_when_fund_check_reports_failure():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.fund_response = {"status": "failure", "remarks": "Invalid_Authentication: DH-901"}
    broker, _ = _broker(fake=fake)

    with pytest.raises(BrokerAuthenticationError):
        broker.connect()
    assert broker.is_connected() is False


def test_connect_fails_when_credentials_missing():
    config = TradingConfig(credentials=BrokerCredentials())
    broker, _ = _broker(config=config)

    with pytest.raises(BrokerConfigError):
        broker.connect()


def test_connect_classifies_a_raw_sdk_exception():
    fake = FakeDhanApi("C123", "tok-abc")

    def _raise():
        raise RuntimeError("Invalid_Authentication: DH-901 token expired")

    fake.get_fund_limits = _raise
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
        broker.get_quote(SYMBOL)


# -- Read-only defaults: TRUE, opposite of AngelOneBroker ------------------------ #
def test_read_only_defaults_to_true_unlike_angelone():
    broker = DhanBroker(_config(live=True), dhan_client_factory=lambda c, t: FakeDhanApi(c, t))
    assert broker.is_read_only is True


def test_read_only_env_var_can_disable_default(monkeypatch):
    monkeypatch.setenv("DHAN_READ_ONLY", "false")
    broker = DhanBroker(_config(live=True), dhan_client_factory=lambda c, t: FakeDhanApi(c, t))
    assert broker.is_read_only is False


def test_place_order_blocked_by_default_read_only():
    broker, fake = _broker(config=_config(live=True), read_only=None)  # picks up the TRUE default
    broker.connect()

    with pytest.raises(ReadOnlyModeError):
        broker.place_order(SYMBOL, OrderSide.SELL, 65, OrderType.MARKET)
    fake.place_order.assert_not_called()


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
    quote = broker.get_quote(SYMBOL)
    assert quote.symbol == SYMBOL
    assert quote.last_price == 133.6
    assert quote.timestamp


def test_get_quote_raises_generic_error_on_malformed_response():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.ticker_response = {"status": "success", "data": {"unexpected": "shape"}}
    broker, _ = _broker(fake=fake)
    broker.connect()
    with pytest.raises(BrokerConnectionError):
        broker.get_quote(SYMBOL)


def test_get_quote_converts_a_broker_reported_failure():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.ticker_response = {"status": "failure", "remarks": "symbol not found"}
    broker, _ = _broker(fake=fake)
    broker.connect()
    with pytest.raises(BrokerConnectionError):
        broker.get_quote(SYMBOL)


# -- Instrument lookup ------------------------------------------------------------- #
def test_resolve_instrument_returns_exchange_segment_and_security_id():
    broker, _ = _broker()
    broker.connect()
    exchange_segment, security_id = broker.resolve_instrument(SYMBOL)
    assert exchange_segment == "NSE_FNO"
    assert security_id == "99999"


# -- Orders: translation (read_only=False + live=True to exercise the real path) --- #
def test_market_order_translation():
    broker, fake = _broker(config=_config(live=True))
    broker.connect()

    result = broker.place_order(SYMBOL, OrderSide.BUY, 65, OrderType.MARKET)

    call = fake.place_order.call_args.kwargs
    assert call["transaction_type"] == "BUY"
    assert call["order_type"] == "MARKET"
    assert call["quantity"] == 65
    assert call["price"] == 0
    assert call["exchange_segment"] == "NSE_FNO"
    assert call["security_id"] == "99999"
    assert result.status == "OPEN"
    assert result.order_id == "DH-1"


def test_limit_order_translation_with_price_and_sell_side():
    broker, fake = _broker(config=_config(live=True))
    broker.connect()

    broker.place_order(SYMBOL, OrderSide.SELL, 65, OrderType.LIMIT, limit_price=132.35)

    call = fake.place_order.call_args.kwargs
    assert call["transaction_type"] == "SELL"
    assert call["order_type"] == "LIMIT"
    assert call["price"] == 132.35
    assert call["product_type"] == "INTRADAY"


def test_limit_order_without_price_raises():
    broker, _fake = _broker(config=_config(live=True))
    broker.connect()
    with pytest.raises(ValueError):
        broker.place_order(SYMBOL, OrderSide.BUY, 65, OrderType.LIMIT, limit_price=None)


def test_place_order_refuses_when_not_live():
    broker, _fake = _broker(config=_config(live=False))
    broker.connect()
    with pytest.raises(LiveTradingDisabledError):
        broker.place_order(SYMBOL, OrderSide.BUY, 65, OrderType.MARKET)


def test_rejected_order_returns_a_rejected_result_not_an_exception():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.place_order_response = {"orderStatus": "REJECTED"}  # no orderId
    broker, _ = _broker(config=_config(live=True), fake=fake)
    broker.connect()

    result = broker.place_order(SYMBOL, OrderSide.BUY, 65, OrderType.MARKET)

    assert result.status == "REJECTED"
    assert result.order_id == ""


# -- Order lifecycle -------------------------------------------------------------- #
def test_get_order_maps_traded_status():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.orders["DH-1"] = {"orderId": "DH-1", "orderStatus": "TRADED", "quantity": "65", "filledQty": "65"}
    broker, _ = _broker(fake=fake)
    broker.connect()

    state = broker.get_order("DH-1")

    assert state == OrderState(order_id="DH-1", status="COMPLETE", filled_quantity=65, remaining_quantity=0)


def test_get_order_maps_pending_and_transit_to_open():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.orders["DH-1"] = {"orderId": "DH-1", "orderStatus": "TRANSIT", "quantity": "65", "filledQty": "0"}
    broker, _ = _broker(fake=fake)
    broker.connect()

    state = broker.get_order("DH-1")

    assert state.status == "OPEN"
    assert state.remaining_quantity == 65


def test_get_order_reports_partial_fill():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.orders["DH-1"] = {"orderId": "DH-1", "orderStatus": "PENDING", "quantity": "65", "filledQty": "20"}
    broker, _ = _broker(fake=fake)
    broker.connect()

    state = broker.get_order("DH-1")

    assert state.filled_quantity == 20
    assert state.remaining_quantity == 45


def test_get_order_unknown_id_returns_unknown_status():
    broker, _ = _broker()
    broker.connect()
    state = broker.get_order("NO-SUCH-ID")
    assert state.status == "UNKNOWN"


def test_get_order_book_lists_all_orders():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.orders["DH-1"] = {"orderId": "DH-1", "orderStatus": "TRADED", "quantity": "65", "filledQty": "65"}
    fake.orders["DH-2"] = {"orderId": "DH-2", "orderStatus": "PENDING", "quantity": "65", "filledQty": "0"}
    broker, _ = _broker(fake=fake)
    broker.connect()

    book = broker.get_order_book()

    assert len(book) == 2


def test_get_open_orders_excludes_terminal_statuses():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.orders["DH-1"] = {"orderId": "DH-1", "orderStatus": "TRADED", "quantity": "65", "filledQty": "65"}
    fake.orders["DH-2"] = {"orderId": "DH-2", "orderStatus": "PENDING", "quantity": "65", "filledQty": "0"}
    broker, _ = _broker(fake=fake)
    broker.connect()

    open_orders = broker.get_open_orders()

    assert [s.order_id for s in open_orders] == ["DH-2"]


def test_modify_order_reuses_order_book_row():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.orders["DH-1"] = {"orderId": "DH-1", "orderStatus": "PENDING", "quantity": "65", "filledQty": "0", "orderType": "LIMIT", "validity": "DAY"}
    broker, _ = _broker(config=_config(live=True), fake=fake)
    broker.connect()

    ok = broker.modify_order("DH-1", 30, 130.0)

    assert ok is True


def test_modify_order_unknown_id_raises():
    broker, _ = _broker(config=_config(live=True))
    broker.connect()
    with pytest.raises(BrokerConnectionError):
        broker.modify_order("NO-SUCH-ID", 10, 100.0)


def test_modify_order_blocked_in_read_only_mode():
    broker, _ = _broker(config=_config(live=True), read_only=True)
    broker.connect()
    with pytest.raises(ReadOnlyModeError):
        broker.modify_order("ANY-ID", 10, 100.0)


def test_cancel_order():
    broker, _fake = _broker(config=_config(live=True))
    broker.connect()
    assert broker.cancel_order("DH-1") is True


def test_cancel_order_refuses_when_not_live():
    broker, _fake = _broker(config=_config(live=False))
    broker.connect()
    with pytest.raises(LiveTradingDisabledError):
        broker.cancel_order("DH-1")


def test_cancel_order_blocked_in_read_only_mode():
    broker, fake = _broker(config=_config(live=True), read_only=True)
    broker.connect()
    with pytest.raises(ReadOnlyModeError):
        broker.cancel_order("DH-1")
    fake.cancel_order.assert_not_called()


# -- Positions ------------------------------------------------------------------- #
def test_get_positions_normalization():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.positions_response = {
        "status": "success",
        "data": [{"tradingSymbol": SYMBOL, "netQty": "-65", "costPrice": "125.5", "lastTradedPrice": "118.0", "realizedProfit": "0", "unrealizedProfit": "487.5"}],
    }
    broker, _ = _broker(fake=fake)
    broker.connect()

    positions = broker.get_positions()

    assert len(positions) == 1
    assert positions[0].symbol == SYMBOL
    assert positions[0].quantity == -65
    assert positions[0].pnl == 487.5


def test_get_positions_skips_malformed_rows():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.positions_response = {"status": "success", "data": [{"missing": "tradingSymbol"}]}
    broker, _ = _broker(fake=fake)
    broker.connect()
    assert broker.get_positions() == []


def test_get_positions_allows_empty_data():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.positions_response = {"status": "success", "data": None}
    broker, _ = _broker(fake=fake)
    broker.connect()
    assert broker.get_positions() == []


# -- Errors ------------------------------------------------------------------------ #
def test_rate_limit_error_is_classified():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.place_order = MagicMock(side_effect=RuntimeError("DH-909 rate limit exceeded"))
    broker, _ = _broker(config=_config(live=True), fake=fake)
    broker.connect()

    with pytest.raises(BrokerRateLimitError):
        broker.place_order(SYMBOL, OrderSide.BUY, 65, OrderType.MARKET)


def test_timeout_is_classified_as_connection_error():
    fake = FakeDhanApi("C123", "tok-abc")

    def _timeout():
        raise TimeoutError("Request timed out")

    fake.get_order_list = _timeout
    broker, _ = _broker(fake=fake)
    broker.connect()

    with pytest.raises(BrokerConnectionError):
        broker.get_order_book()


def test_generic_connection_error_for_unrecognized_exception():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.place_order = MagicMock(side_effect=RuntimeError("some transient network blip"))
    broker, _ = _broker(config=_config(live=True), fake=fake)
    broker.connect()

    with pytest.raises(BrokerConnectionError):
        broker.place_order(SYMBOL, OrderSide.BUY, 65, OrderType.MARKET)


# -- Security: no secrets leak ------------------------------------------------------ #
def test_repr_never_exposes_secrets():
    broker, _ = _broker()
    broker.connect()
    text = repr(broker)
    assert "tok-abc" not in text
    assert "C123" not in text


def test_exception_messages_never_include_credentials():
    fake = FakeDhanApi("C123", "tok-abc")
    fake.fund_response = {"status": "failure", "remarks": "Invalid_Authentication: DH-901"}
    broker, _ = _broker(fake=fake)

    with pytest.raises(BrokerAuthenticationError) as excinfo:
        broker.connect()

    assert "tok-abc" not in str(excinfo.value)
    assert "C123" not in str(excinfo.value)


def test_config_error_message_never_includes_partial_credentials():
    config = TradingConfig(credentials=BrokerCredentials(dhan_client_id="C123"))
    broker, _ = _broker(config=config)

    with pytest.raises(BrokerConfigError) as excinfo:
        broker.connect()

    assert "C123" not in str(excinfo.value)
