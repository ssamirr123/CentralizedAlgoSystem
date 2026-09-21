"""trading/common/brokers/shadow_broker.py -- ShadowBroker exercised
directly. No network, no SDK, no credentials anywhere -- structural
safety, not configuration-based safety (see the module docstring)."""
from __future__ import annotations

from trading.common.broker import OrderSide, OrderType
from trading.common.brokers.shadow_broker import ShadowBroker
from trading.common.order_intent import OrderIntent


def _intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id="DoubleStraddelAlgo",
        account_id="SHADOW_MAIN",
        symbol="NIFTY19MAY2623700CE",
        exchange="NFO",
        side=OrderSide.SELL,
        quantity=65,
        order_type=OrderType.LIMIT,
        limit_price=125.5,
        reason="ENTRY",
    )
    fields.update(overrides)
    return OrderIntent(**fields)


def _broker() -> ShadowBroker:
    broker = ShadowBroker()
    broker.connect()
    return broker


# --------------------------------------------------------------------------- #
# Connection lifecycle / structural safety
# --------------------------------------------------------------------------- #
def test_connect_disconnect_never_touch_network():
    broker = ShadowBroker()
    assert broker.is_connected() is False
    broker.connect()
    assert broker.is_connected() is True
    broker.disconnect()
    assert broker.is_connected() is False


def test_shadow_broker_has_no_broker_sdk_imports():
    """Structural guard: this module must never import a broker SDK."""
    import inspect

    import trading.common.brokers.shadow_broker as module

    source = inspect.getsource(module)
    for forbidden in ("SmartApi", "SmartConnect", "breeze_connect", "requests", "socket", "urllib"):
        assert forbidden not in source


# --------------------------------------------------------------------------- #
# BUY / SELL, CE / PE, quantity preserved
# --------------------------------------------------------------------------- #
def test_buy_order_fills():
    broker = _broker()
    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.BUY, 65, OrderType.MARKET)
    assert result.status == "COMPLETE"
    assert result.side == OrderSide.BUY


def test_sell_order_fills():
    broker = _broker()
    result = broker.place_order("NIFTY19MAY2623700PE", OrderSide.SELL, 65, OrderType.MARKET)
    assert result.status == "COMPLETE"
    assert result.side == OrderSide.SELL


def test_ce_and_pe_symbols_tracked_independently():
    broker = _broker()
    broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)
    broker.place_order("NIFTY19MAY2623700PE", OrderSide.SELL, 65, OrderType.MARKET)

    positions = {p.symbol: p for p in broker.get_positions()}
    assert "NIFTY19MAY2623700CE" in positions
    assert "NIFTY19MAY2623700PE" in positions
    assert positions["NIFTY19MAY2623700CE"].quantity == -65
    assert positions["NIFTY19MAY2623700PE"].quantity == -65


def test_quantity_is_preserved_in_the_result():
    broker = _broker()
    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 40, OrderType.MARKET)
    assert result.quantity == 40


def test_zero_or_negative_quantity_is_rejected():
    broker = _broker()
    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 0, OrderType.MARKET)
    assert result.status == "REJECTED"


# --------------------------------------------------------------------------- #
# Simulated order acceptance / rejection
# --------------------------------------------------------------------------- #
def test_simulated_order_id_is_present_and_prefixed():
    broker = _broker()
    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)
    assert result.order_id.startswith("SHADOW-")


def test_configured_rejection_produces_no_order_id_and_no_position():
    broker = _broker()
    broker.configure_next_order("NIFTY19MAY2623700CE", "REJECTED", reason="margin insufficient")

    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)

    assert result.status == "REJECTED"
    assert result.order_id == ""
    assert result.message == "margin insufficient"
    assert broker.get_positions() == []


def test_rejection_configuration_is_consumed_exactly_once():
    broker = _broker()
    broker.configure_next_order("NIFTY19MAY2623700CE", "REJECTED")

    first = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)
    second = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)

    assert first.status == "REJECTED"
    assert second.status == "COMPLETE"  # back to default behavior


# --------------------------------------------------------------------------- #
# Simulated fill
# --------------------------------------------------------------------------- #
def test_market_order_fills_at_the_synthetic_quote_price():
    broker = _broker()
    quote = broker.get_quote("NIFTY19MAY2623700CE")

    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)

    assert result.status == "COMPLETE"
    position = broker.get_positions()[0]
    assert position.average_price == quote.last_price


def test_limit_order_fills_at_the_limit_price():
    broker = _broker()
    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.LIMIT, limit_price=125.5)

    assert result.status == "COMPLETE"
    position = broker.get_positions()[0]
    assert position.average_price == 125.5


def test_get_order_reflects_a_complete_fill():
    broker = _broker()
    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)

    state = broker.get_order(result.order_id)

    assert state.status == "COMPLETE"
    assert state.filled_quantity == 65
    assert state.remaining_quantity == 0


# --------------------------------------------------------------------------- #
# Partial fill
# --------------------------------------------------------------------------- #
def test_configured_partial_fill_leaves_order_open_with_remainder():
    broker = _broker()
    broker.configure_next_order("NIFTY19MAY2623700CE", "PARTIAL", fill_ratio=0.4)

    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)

    assert result.status == "OPEN"
    state = broker.get_order(result.order_id)
    assert 0 < state.filled_quantity < 65
    assert state.remaining_quantity == 65 - state.filled_quantity
    assert state.status == "OPEN"


def test_partial_fill_updates_position_for_the_filled_portion_only():
    broker = _broker()
    broker.configure_next_order("NIFTY19MAY2623700CE", "PARTIAL", fill_ratio=0.5)

    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 60, OrderType.MARKET)

    state = broker.get_order(result.order_id)
    position = broker.get_positions()[0]
    assert position.quantity == -state.filled_quantity


def test_fill_pending_order_completes_the_remainder():
    broker = _broker()
    broker.configure_next_order("NIFTY19MAY2623700CE", "PARTIAL", fill_ratio=0.5)
    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 60, OrderType.MARKET)
    remaining_before = broker.get_order(result.order_id).remaining_quantity

    broker.fill_pending_order(result.order_id)

    final_state = broker.get_order(result.order_id)
    assert final_state.status == "COMPLETE"
    assert final_state.filled_quantity == 60
    assert remaining_before > 0


def test_open_order_never_fills_on_its_own():
    broker = _broker()
    broker.configure_next_order("NIFTY19MAY2623700CE", "OPEN")

    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)

    assert result.status == "OPEN"
    state = broker.get_order(result.order_id)
    assert state.filled_quantity == 0
    assert state.remaining_quantity == 65


def test_cancel_order_on_a_still_open_order():
    broker = _broker()
    broker.configure_next_order("NIFTY19MAY2623700CE", "OPEN")
    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)

    assert broker.cancel_order(result.order_id) is True
    assert broker.get_order(result.order_id).status == "CANCELLED"


def test_cancel_order_on_an_already_complete_order_is_a_no_op():
    broker = _broker()
    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)

    assert broker.cancel_order(result.order_id) is False


# --------------------------------------------------------------------------- #
# Position update / average price / P&L
# --------------------------------------------------------------------------- #
def test_position_update_after_sell_then_buy_to_cover_realizes_pnl():
    broker = _broker()
    broker.configure_next_order("NIFTY19MAY2623700CE", "FILLED")
    broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.LIMIT, limit_price=125.5)  # entry, short
    broker.place_order("NIFTY19MAY2623700CE", OrderSide.BUY, 65, OrderType.LIMIT, limit_price=100.0)  # exit, buy to cover

    position = broker.get_positions()[0]
    assert position.quantity == 0
    # Short entry@125.5, cover@100 -> profit of 25.5/lot * 65
    assert position.pnl == round((125.5 - 100.0) * 65, 2)


def test_position_average_price_on_adding_to_the_same_side():
    broker = _broker()
    broker.place_order("NIFTY19MAY2623700CE", OrderSide.BUY, 50, OrderType.LIMIT, limit_price=100.0)
    broker.place_order("NIFTY19MAY2623700CE", OrderSide.BUY, 50, OrderType.LIMIT, limit_price=120.0)

    position = broker.get_positions()[0]
    assert position.quantity == 100
    assert position.average_price == 110.0  # (100*50 + 120*50) / 100


def test_position_last_price_reflects_the_most_recent_fill():
    broker = _broker()
    broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.LIMIT, limit_price=125.5)

    position = broker.get_positions()[0]
    assert position.last_price == 125.5


# --------------------------------------------------------------------------- #
# Duplicate intent (idempotency)
# --------------------------------------------------------------------------- #
def test_duplicate_intent_with_same_idempotency_key_produces_one_order():
    broker = _broker()
    intent = _intent(idempotency_key="hedge-entry-ce-2026-09-15")

    first = broker.simulate_intent(intent)
    second = broker.simulate_intent(intent)

    assert first.order_id == second.order_id
    assert len(broker.get_positions()) == 1
    assert broker.get_positions()[0].quantity == -65  # not -130


def test_distinct_idempotency_keys_produce_distinct_orders():
    broker = _broker()
    first_intent = _intent(idempotency_key="entry-1")
    second_intent = _intent(idempotency_key="entry-2")

    first = broker.simulate_intent(first_intent)
    second = broker.simulate_intent(second_intent)

    assert first.order_id != second.order_id
    assert broker.get_positions()[0].quantity == -130


def test_empty_idempotency_key_never_dedupes():
    broker = _broker()
    intent = _intent(idempotency_key="")

    first = broker.simulate_intent(intent)
    second = broker.simulate_intent(intent)

    assert first.order_id != second.order_id


def test_simulate_intent_reuses_place_order_translation():
    broker = _broker()
    intent = _intent(side=OrderSide.BUY, quantity=30, order_type=OrderType.MARKET, limit_price=None)

    result = broker.simulate_intent(intent)

    assert result.side == OrderSide.BUY
    assert result.quantity == 30
    assert result.status == "COMPLETE"


# --------------------------------------------------------------------------- #
# Order modification (simulated)
# --------------------------------------------------------------------------- #
def test_modify_order_reprices_the_remaining_quantity():
    broker = _broker()
    broker.configure_next_order("NIFTY19MAY2623700CE", "PARTIAL", fill_ratio=0.5)
    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 60, OrderType.LIMIT, limit_price=125.5)
    remaining = broker.get_order(result.order_id).remaining_quantity

    ok = broker.modify_order(result.order_id, remaining, 130.0)

    assert ok is True
    state = broker.get_order(result.order_id)
    assert state.remaining_quantity == remaining
    assert state.status == "OPEN"


def test_modify_order_on_unknown_id_returns_false():
    broker = _broker()
    assert broker.modify_order("NO-SUCH-ID", 10, 100.0) is False


def test_modify_order_on_a_complete_order_returns_false():
    broker = _broker()
    result = broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)
    assert broker.modify_order(result.order_id, 65, 100.0) is False


# --------------------------------------------------------------------------- #
# Order book listing
# --------------------------------------------------------------------------- #
def test_get_order_book_lists_every_simulated_order():
    broker = _broker()
    broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)
    broker.configure_next_order("NIFTY19MAY2623700PE", "OPEN")
    broker.place_order("NIFTY19MAY2623700PE", OrderSide.SELL, 65, OrderType.MARKET)

    book = broker.get_order_book()

    assert len(book) == 2


def test_get_open_orders_excludes_complete_orders():
    broker = _broker()
    broker.place_order("NIFTY19MAY2623700CE", OrderSide.SELL, 65, OrderType.MARKET)
    broker.configure_next_order("NIFTY19MAY2623700PE", "OPEN")
    open_result = broker.place_order("NIFTY19MAY2623700PE", OrderSide.SELL, 65, OrderType.MARKET)

    open_orders = broker.get_open_orders()

    assert [s.order_id for s in open_orders] == [open_result.order_id]
