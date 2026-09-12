"""trading/common/order_intent.py -- OrderIntent is pure data, no broker
SDK types, no network calls."""
from __future__ import annotations

from trading.common.broker import OrderSide, OrderType
from trading.common.instrument import Instrument
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


def test_instrument_defaults_to_none_and_can_be_supplied():
    intent = OrderIntent(**_base_kwargs())
    assert intent.instrument is None

    inst = Instrument(symbol="NIFTY24950CE", exchange="NFO", option_type="CE")
    intent_with_instrument = OrderIntent(**_base_kwargs(instrument=inst))
    assert intent_with_instrument.instrument is inst


def test_correlation_id_auto_generated_and_unique():
    a = OrderIntent(**_base_kwargs())
    b = OrderIntent(**_base_kwargs())
    assert a.correlation_id
    assert a.correlation_id != b.correlation_id


def test_correlation_id_can_be_supplied_to_link_related_intents():
    shared = "CORR-multi-leg-entry-1"
    leg1 = OrderIntent(**_base_kwargs(correlation_id=shared))
    leg2 = OrderIntent(**_base_kwargs(symbol="NIFTY24950PE", correlation_id=shared))
    assert leg1.correlation_id == leg2.correlation_id == shared


def test_idempotency_key_defaults_empty_and_is_not_auto_generated():
    a = OrderIntent(**_base_kwargs())
    b = OrderIntent(**_base_kwargs())
    assert a.idempotency_key == ""
    assert b.idempotency_key == ""  # NOT auto-generated/unique, unlike client_order_id/correlation_id


def test_idempotency_key_can_be_supplied_explicitly():
    intent = OrderIntent(**_base_kwargs(idempotency_key="strategy-x-symbol-y-2026-09-15"))
    assert intent.idempotency_key == "strategy-x-symbol-y-2026-09-15"


def test_created_at_is_a_populated_iso_timestamp():
    intent = OrderIntent(**_base_kwargs())
    assert intent.created_at
    # Must be parseable as an ISO 8601 timestamp.
    from datetime import datetime

    datetime.fromisoformat(intent.created_at)
