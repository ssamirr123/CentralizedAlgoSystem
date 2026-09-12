"""trading/common/brokers/icici_breeze.py -- ICICIBreezeBroker exercised
entirely against a FakeBreezeApi double. No real network call, no real
credentials -- same mocking discipline as
tests/common/test_angelone_broker.py and tests/common/test_dhan_broker.py.
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
from trading.common.brokers.icici_breeze import AccountInfo, BreezeInstrument, FundsSnapshot, ICICIBreezeBroker
from trading.common.config import BrokerCredentials, TradingConfig
from trading.common.execution import OrderState


class FakeBreezeApi:
    """Mirrors breeze_connect.BreezeConnect's client surface -- no network,
    no real SDK object."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.funds_response = {"Success": {"unallocated_balance": 50000.0, "block_by_trade_balance": 1000.0}, "Status": 200, "Error": None}
        self.customer_response = {"Success": {"idirect_userid": "ICICI1", "idirect_user_name": "Test User", "email_id": "t@example.com"}, "Status": 200, "Error": None}
        self.positions_response = {"Success": [], "Status": 200, "Error": None}
        self.orders: dict[str, dict] = {}
        self.quote_response = {"Success": [{"ltp": "133.6"}], "Status": 200, "Error": None}
        self.place_order_response = {"Success": {"order_id": "IC-1"}, "Status": 200, "Error": None}
        self.place_order = MagicMock(name="place_order", side_effect=lambda **k: self.place_order_response)
        self.cancel_order = MagicMock(name="cancel_order", return_value={"Success": {}, "Status": 200, "Error": None})

    def generate_session(self, api_secret, session_token):
        return {"Success": {}, "Status": 200, "Error": None}

    def get_quotes(self, **kwargs):
        return self.quote_response

    def get_funds(self):
        return self.funds_response

    def get_customer_details(self, api_session):
        return self.customer_response

    def get_portfolio_positions(self):
        return self.positions_response

    def get_order_list(self, exchange_code, from_date, to_date):
        return {"Success": list(self.orders.values()), "Status": 200, "Error": None}

    def modify_order(self, order_id, exchange_code, order_type, quantity, price, validity):
        return {"Success": {}, "Status": 200, "Error": None}


def _config(live: bool = False) -> TradingConfig:
    return TradingConfig(
        trading_mode="live" if live else "paper",
        credentials=BrokerCredentials(
            icici_breeze_api_key="ak", icici_breeze_api_secret="sec", icici_breeze_session_token="tok",
        ),
    )


def _broker(config: TradingConfig | None = None, fake: FakeBreezeApi | None = None, read_only: bool | None = False):
    fake = fake or FakeBreezeApi("ak")
    broker = ICICIBreezeBroker(
        config or _config(),
        breeze_client_factory=lambda api_key: fake,
        read_only=read_only,
    )
    return broker, fake


SYMBOL = "NIFTY15SEP2623400CE"


# -- Authentication ----------------------------------------------------------- #
def test_connect_succeeds_with_mocked_breeze_client():
    broker, _fake = _broker()
    broker.connect()
    assert broker.is_connected() is True


def test_connect_fails_when_generate_session_raises():
    fake = FakeBreezeApi("ak")

    def _raise(api_secret, session_token):
        raise RuntimeError("Invalid session token")

    fake.generate_session = _raise
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
    fake = FakeBreezeApi("ak")

    def _raise(api_secret, session_token):
        raise RuntimeError("Unauthorized: invalid public key")

    fake.generate_session = _raise
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


# -- Read-only defaults: TRUE, matching DhanBroker, unlike AngelOneBroker -------- #
def test_read_only_defaults_to_true_unlike_angelone():
    broker = ICICIBreezeBroker(_config(live=True), breeze_client_factory=lambda k: FakeBreezeApi(k))
    assert broker.is_read_only is True


def test_read_only_env_var_can_disable_default(monkeypatch):
    monkeypatch.setenv("ICICI_BREEZE_READ_ONLY", "false")
    broker = ICICIBreezeBroker(_config(live=True), breeze_client_factory=lambda k: FakeBreezeApi(k))
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
    assert info == AccountInfo(client_id="ICICI1", name="Test User", email="t@example.com")


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
    fake = FakeBreezeApi("ak")
    fake.quote_response = {"Success": [{"unexpected": "shape"}], "Status": 200, "Error": None}
    broker, _ = _broker(fake=fake)
    broker.connect()
    with pytest.raises(BrokerConnectionError):
        broker.get_quote(SYMBOL)


def test_get_quote_converts_a_broker_reported_failure():
    fake = FakeBreezeApi("ak")
    fake.quote_response = {"Success": None, "Status": 400, "Error": "symbol not found"}
    broker, _ = _broker(fake=fake)
    broker.connect()
    with pytest.raises(BrokerConnectionError):
        broker.get_quote(SYMBOL)


# -- Instrument resolution (parsed directly from the symbol -- no network) ------- #
def test_resolve_instrument_parses_the_angel_style_symbol():
    broker, _ = _broker()
    broker.connect()
    inst = broker.resolve_instrument(SYMBOL)
    assert inst == BreezeInstrument(
        stock_code="NIFTY", exchange_code="NFO", product_type="options",
        expiry_date="2026-09-15T00:00:00.000Z", right="call", strike_price="23400",
    )


def test_resolve_instrument_handles_put_options():
    broker, _ = _broker()
    broker.connect()
    inst = broker.resolve_instrument("NIFTY15SEP2623400PE")
    assert inst.right == "put"


def test_resolve_instrument_rejects_unparseable_symbol():
    broker, _ = _broker()
    broker.connect()
    with pytest.raises(ValueError):
        broker.resolve_instrument("NOT-A-VALID-SYMBOL")


# -- Orders: translation (read_only=False + live=True to exercise the real path) --- #
def test_market_order_translation():
    broker, fake = _broker(config=_config(live=True))
    broker.connect()

    result = broker.place_order(SYMBOL, OrderSide.BUY, 65, OrderType.MARKET)

    call = fake.place_order.call_args.kwargs
    assert call["action"] == "buy"
    assert call["order_type"] == "market"
    assert call["quantity"] == "65"
    assert call["price"] == "0"
    assert call["stock_code"] == "NIFTY"
    assert call["right"] == "call"
    assert call["strike_price"] == "23400"
    assert result.status == "OPEN"
    assert result.order_id == "IC-1"


def test_limit_order_translation_with_price_and_sell_side():
    broker, fake = _broker(config=_config(live=True))
    broker.connect()

    broker.place_order(SYMBOL, OrderSide.SELL, 65, OrderType.LIMIT, limit_price=132.35)

    call = fake.place_order.call_args.kwargs
    assert call["action"] == "sell"
    assert call["order_type"] == "limit"
    assert call["price"] == "132.35"
    assert call["product"] == "options"


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
    fake = FakeBreezeApi("ak")
    fake.place_order_response = {"Success": {}, "Status": 200, "Error": None}  # no order_id
    broker, _ = _broker(config=_config(live=True), fake=fake)
    broker.connect()

    result = broker.place_order(SYMBOL, OrderSide.BUY, 65, OrderType.MARKET)

    assert result.status == "REJECTED"
    assert result.order_id == ""


# -- Order lifecycle -------------------------------------------------------------- #
def test_get_order_maps_executed_status():
    fake = FakeBreezeApi("ak")
    fake.orders["IC-1"] = {"order_id": "IC-1", "status": "Executed", "quantity": "65", "pending_quantity": "0"}
    broker, _ = _broker(fake=fake)
    broker.connect()

    state = broker.get_order("IC-1")

    assert state == OrderState(order_id="IC-1", status="COMPLETE", filled_quantity=65, remaining_quantity=0)


def test_get_order_maps_ordered_and_pending_to_open():
    fake = FakeBreezeApi("ak")
    fake.orders["IC-1"] = {"order_id": "IC-1", "status": "Ordered", "quantity": "65", "pending_quantity": "65"}
    broker, _ = _broker(fake=fake)
    broker.connect()

    state = broker.get_order("IC-1")

    assert state.status == "OPEN"
    assert state.remaining_quantity == 65


def test_get_order_reports_partial_fill():
    fake = FakeBreezeApi("ak")
    fake.orders["IC-1"] = {"order_id": "IC-1", "status": "Ordered", "quantity": "65", "pending_quantity": "45"}
    broker, _ = _broker(fake=fake)
    broker.connect()

    state = broker.get_order("IC-1")

    assert state.filled_quantity == 20
    assert state.remaining_quantity == 45


def test_get_order_unknown_id_returns_unknown_status():
    broker, _ = _broker()
    broker.connect()
    state = broker.get_order("NO-SUCH-ID")
    assert state.status == "UNKNOWN"


def test_get_order_book_lists_all_orders():
    fake = FakeBreezeApi("ak")
    fake.orders["IC-1"] = {"order_id": "IC-1", "status": "Executed", "quantity": "65", "pending_quantity": "0"}
    fake.orders["IC-2"] = {"order_id": "IC-2", "status": "Ordered", "quantity": "65", "pending_quantity": "65"}
    broker, _ = _broker(fake=fake)
    broker.connect()

    book = broker.get_order_book()

    assert len(book) == 2


def test_get_open_orders_excludes_terminal_statuses():
    fake = FakeBreezeApi("ak")
    fake.orders["IC-1"] = {"order_id": "IC-1", "status": "Executed", "quantity": "65", "pending_quantity": "0"}
    fake.orders["IC-2"] = {"order_id": "IC-2", "status": "Ordered", "quantity": "65", "pending_quantity": "65"}
    broker, _ = _broker(fake=fake)
    broker.connect()

    open_orders = broker.get_open_orders()

    assert [s.order_id for s in open_orders] == ["IC-2"]


def test_modify_order_reuses_order_book_row():
    fake = FakeBreezeApi("ak")
    fake.orders["IC-1"] = {"order_id": "IC-1", "status": "Ordered", "quantity": "65", "pending_quantity": "65"}
    broker, _ = _broker(config=_config(live=True), fake=fake)
    broker.connect()

    ok = broker.modify_order("IC-1", 30, 130.0)

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
    assert broker.cancel_order("IC-1") is True


def test_cancel_order_refuses_when_not_live():
    broker, _fake = _broker(config=_config(live=False))
    broker.connect()
    with pytest.raises(LiveTradingDisabledError):
        broker.cancel_order("IC-1")


def test_cancel_order_blocked_in_read_only_mode():
    broker, fake = _broker(config=_config(live=True), read_only=True)
    broker.connect()
    with pytest.raises(ReadOnlyModeError):
        broker.cancel_order("IC-1")
    fake.cancel_order.assert_not_called()


# -- Positions ------------------------------------------------------------------- #
def test_get_positions_normalization():
    fake = FakeBreezeApi("ak")
    fake.positions_response = {
        "Success": [{"stock_code": "NIFTY", "quantity": "-65", "average_price": "125.5", "ltp": "118.0", "realized_profit": "0", "unrealized_profit": "487.5"}],
        "Status": 200, "Error": None,
    }
    broker, _ = _broker(fake=fake)
    broker.connect()

    positions = broker.get_positions()

    assert len(positions) == 1
    assert positions[0].symbol == "NIFTY"
    assert positions[0].quantity == -65
    assert positions[0].pnl == 487.5


def test_get_positions_skips_malformed_rows():
    fake = FakeBreezeApi("ak")
    fake.positions_response = {"Success": [{"missing": "stock_code"}], "Status": 200, "Error": None}
    broker, _ = _broker(fake=fake)
    broker.connect()
    assert broker.get_positions() == []


def test_get_positions_allows_empty_data():
    fake = FakeBreezeApi("ak")
    fake.positions_response = {"Success": None, "Status": 200, "Error": None}
    broker, _ = _broker(fake=fake)
    broker.connect()
    assert broker.get_positions() == []


# -- Errors ------------------------------------------------------------------------ #
def test_rate_limit_error_is_classified():
    fake = FakeBreezeApi("ak")
    fake.place_order = MagicMock(side_effect=RuntimeError("Too many requests, rate limit exceeded"))
    broker, _ = _broker(config=_config(live=True), fake=fake)
    broker.connect()

    with pytest.raises(BrokerRateLimitError):
        broker.place_order(SYMBOL, OrderSide.BUY, 65, OrderType.MARKET)


def test_timeout_is_classified_as_connection_error():
    fake = FakeBreezeApi("ak")

    def _timeout(exchange_code, from_date, to_date):
        raise TimeoutError("Request timed out")

    fake.get_order_list = _timeout
    broker, _ = _broker(fake=fake)
    broker.connect()

    with pytest.raises(BrokerConnectionError):
        broker.get_order_book()


def test_generic_connection_error_for_unrecognized_exception():
    fake = FakeBreezeApi("ak")
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
    assert "sec" not in text
    assert "tok" not in text


def test_exception_messages_never_include_credentials():
    fake = FakeBreezeApi("ak")

    def _raise(api_secret, session_token):
        raise RuntimeError("Unauthorized")

    fake.generate_session = _raise
    broker, _ = _broker(fake=fake)

    with pytest.raises(BrokerAuthenticationError) as excinfo:
        broker.connect()

    assert "sec" not in str(excinfo.value)
    assert "tok" not in str(excinfo.value)


def test_config_error_message_never_includes_partial_credentials():
    config = TradingConfig(credentials=BrokerCredentials(icici_breeze_api_key="ak"))
    broker, _ = _broker(config=config)

    with pytest.raises(BrokerConfigError) as excinfo:
        broker.connect()

    assert "ak" not in str(excinfo.value)
