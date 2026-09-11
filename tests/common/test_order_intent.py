"""trading/common/order_intent.py -- OrderIntent is pure data, no broker
SDK types, no network calls."""
from __future__ import annotations

from trading.common.broker import OrderSide, OrderType
from trading.common.order_intent import OrderIntent, ProductType, Validity


def _base_kwargs(**overrides):
    fields = dict(
        strategy_id="DoubleStraddelAlgo",
        account_id="PAPER_MAIN",
        symbol="NIFTY24950CE",
        exchange="NFO",
        side=OrderSide.SELL,
        quantity=50,
    )
    fields.update(overrides)
    return fields


def test_enum_fields_accept_enum_members_directly():
    intent = OrderIntent(**_base_kwargs(order_type=OrderType.LIMIT, limit_price=125.5))
    assert intent.side == OrderSide.SELL
    assert intent.order_type == OrderType.LIMIT


def test_enum_fields_accept_plain_strings_and_coerce():
    intent = OrderIntent(**_base_kwargs(side="SELL", order_type="LIMIT", limit_price=125.5))
    assert intent.side == OrderSide.SELL
    assert isinstance(intent.side, OrderSide)
    assert intent.order_type == OrderType.LIMIT
    assert isinstance(intent.order_type, OrderType)


def test_defaults():
    intent = OrderIntent(**_base_kwargs())
    assert intent.order_type == OrderType.MARKET
    assert intent.product_type == ProductType.INTRADAY
    assert intent.validity == Validity.DAY
    assert intent.limit_price is None
    assert intent.trigger_price is None
    assert intent.reason == ""
    assert intent.metadata == {}


def test_client_order_id_auto_generated_and_unique():
    a = OrderIntent(**_base_kwargs())
    b = OrderIntent(**_base_kwargs())
    assert a.client_order_id
    assert b.client_order_id
    assert a.client_order_id != b.client_order_id


def test_client_order_id_can_be_supplied_explicitly():
    intent = OrderIntent(**_base_kwargs(client_order_id="MY-ID-1"))
    assert intent.client_order_id == "MY-ID-1"


def test_metadata_is_independent_per_instance():
    a = OrderIntent(**_base_kwargs())
    b = OrderIntent(**_base_kwargs())
    a.metadata["x"] = 1
    assert b.metadata == {}


def test_intent_has_no_broker_specific_field_names():
    """Structural guard: the dataclass must never grow a field that names a
    broker SDK concept (e.g. a SmartApi/Breeze/Dhan/Shoonya request shape)."""
    forbidden = {"smartapi", "breeze", "dhan", "shoonya", "variety", "producttype", "symboltoken"}
    field_names = {f.lower() for f in OrderIntent.__dataclass_fields__.keys()}
    assert field_names.isdisjoint(forbidden)
