"""Phase 14: LiveCanaryGuard (trading/common/live_canary.py) in isolation
-- no execution engine, no broker, no strategy. See
tests/common/test_live_canary_execution_wiring.py for the end-to-end
pipeline proof."""
from __future__ import annotations

import pytest

from trading.common.alerts import AlertManager
from trading.common.broker import OrderResult, OrderSide, OrderType
from trading.common.live_canary import CanaryLimits, LiveCanaryGuard
from trading.common.order_intent import OrderIntent

ACCOUNT = "ANGEL_CANARY"


def _limits(**overrides) -> CanaryLimits:
    defaults = dict(
        account_id=ACCOUNT, max_order_quantity=5, max_order_value=1000.0,
        max_daily_loss=500.0, max_strategy_loss=500.0, max_orders_per_day=3,
    )
    defaults.update(overrides)
    return CanaryLimits(**defaults)


def _intent(**overrides) -> OrderIntent:
    defaults = dict(
        strategy_id="DoubleStraddelAlgo", account_id=ACCOUNT, symbol="NIFTY15SEP2623400CE", exchange="NFO",
        side=OrderSide.SELL, quantity=1, order_type=OrderType.LIMIT, limit_price=100.0,
        idempotency_key="idem-1",
    )
    defaults.update(overrides)
    return OrderIntent(**defaults)


# --------------------------------------------------------------------------- #
# CanaryLimits: fail-closed construction, no defaults
# --------------------------------------------------------------------------- #
def test_canary_limits_requires_every_field_explicitly():
    with pytest.raises(TypeError):
        CanaryLimits()  # type: ignore[call-arg]


def test_canary_limits_rejects_empty_account_id():
    with pytest.raises(ValueError):
        _limits(account_id="")


@pytest.mark.parametrize("field", ["max_order_quantity", "max_order_value", "max_daily_loss", "max_strategy_loss", "max_orders_per_day"])
def test_canary_limits_rejects_non_positive_values(field):
    with pytest.raises(ValueError):
        _limits(**{field: 0})


@pytest.mark.parametrize("field", ["max_order_quantity", "max_order_value", "max_daily_loss", "max_strategy_loss", "max_orders_per_day"])
def test_canary_limits_rejects_malformed_non_numeric_values_as_valueerror(field):
    """Phase 14.7 regression: a malformed (non-numeric) value used to
    raise a raw TypeError instead of the ValueError this class's own
    docstring promises -- fixed to raise ValueError consistently."""
    with pytest.raises(ValueError):
        _limits(**{field: "not-a-number"})


# --------------------------------------------------------------------------- #
# authorize(): the 9 pre-broker checks
# --------------------------------------------------------------------------- #
def test_authorize_approves_a_well_formed_tiny_intent():
    guard = LiveCanaryGuard(_limits())
    result = guard.authorize(_intent())
    assert result.allowed is True
    assert result.status == "AUTHORIZED"
    assert all(c.passed for c in result.checks)


def test_check_1_dedicated_account_rejects_a_different_account():
    guard = LiveCanaryGuard(_limits())
    result = guard.authorize(_intent(account_id="SOME_OTHER_ACCOUNT"))
    assert result.allowed is False
    assert "DEDICATED_ACCOUNT" in result.reason


def test_check_2_max_quantity_rejects_oversized_order():
    guard = LiveCanaryGuard(_limits(max_order_quantity=1))
    result = guard.authorize(_intent(quantity=2))
    assert result.allowed is False
    assert "MAX_ORDER_QUANTITY" in result.reason


def test_check_3_max_order_value_rejects_expensive_order():
    guard = LiveCanaryGuard(_limits(max_order_value=50.0))
    result = guard.authorize(_intent(quantity=1, limit_price=100.0))  # value 100 > limit 50
    assert result.allowed is False
    assert "MAX_ORDER_VALUE" in result.reason


def test_check_3_max_order_value_uses_reference_price_when_no_limit_price():
    guard = LiveCanaryGuard(_limits(max_order_value=50.0))
    intent = _intent(order_type=OrderType.MARKET, limit_price=None)
    result = guard.authorize(intent, reference_price=100.0)
    assert result.allowed is False
    assert "MAX_ORDER_VALUE" in result.reason


def test_check_3_rejects_when_no_price_is_available_at_all():
    guard = LiveCanaryGuard(_limits())
    intent = _intent(order_type=OrderType.MARKET, limit_price=None)
    result = guard.authorize(intent)  # no reference_price either
    assert result.allowed is False
    assert "MAX_ORDER_VALUE" in result.reason


def test_check_4_max_daily_loss_rejects_when_breached():
    guard = LiveCanaryGuard(_limits(max_daily_loss=100.0))
    result = guard.authorize(_intent(), daily_pnl=-150.0)
    assert result.allowed is False
    assert "MAX_DAILY_LOSS" in result.reason


def test_check_5_max_strategy_loss_rejects_when_breached():
    guard = LiveCanaryGuard(_limits(max_strategy_loss=100.0))
    result = guard.authorize(_intent(), strategy_pnl=-150.0)
    assert result.allowed is False
    assert "MAX_STRATEGY_LOSS" in result.reason


def test_check_6_max_orders_per_day_rejects_once_limit_reached():
    guard = LiveCanaryGuard(_limits(max_orders_per_day=2))
    guard.authorize(_intent(idempotency_key="k1"))
    guard.authorize(_intent(idempotency_key="k2"))
    result = guard.authorize(_intent(idempotency_key="k3"))
    assert result.allowed is False
    assert "MAX_ORDERS_PER_DAY" in result.reason


def test_check_6_a_rejected_intent_does_not_consume_the_daily_budget():
    guard = LiveCanaryGuard(_limits(max_orders_per_day=1))
    # This one is rejected for an unrelated reason (wrong account) --
    # must not consume the day's one-order budget.
    guard.authorize(_intent(account_id="WRONG", idempotency_key="k1"))
    result = guard.authorize(_intent(idempotency_key="k2"))
    assert result.allowed is True


def test_check_7_kill_switch_blocks_authorization():
    guard = LiveCanaryGuard(_limits())
    guard.engage_kill_switch(reason="manual halt")
    result = guard.authorize(_intent())
    assert result.allowed is False
    assert "KILL_SWITCH" in result.reason

    guard.disengage_kill_switch()
    result2 = guard.authorize(_intent(idempotency_key="idem-2"))
    assert result2.allowed is True


def test_check_8_duplicate_order_protection_rejects_a_repeated_key():
    guard = LiveCanaryGuard(_limits())
    guard.authorize(_intent(idempotency_key="same-key"))
    result = guard.authorize(_intent(idempotency_key="same-key"))
    assert result.allowed is False
    assert "DUPLICATE_ORDER_PROTECTION" in result.reason


def test_check_9_idempotency_is_mandatory_unlike_risk_manager():
    guard = LiveCanaryGuard(_limits())
    result = guard.authorize(_intent(idempotency_key=""))
    assert result.allowed is False
    assert "IDEMPOTENCY_REQUIRED" in result.reason


# --------------------------------------------------------------------------- #
# 12: emergency shutdown
# --------------------------------------------------------------------------- #
def test_check_12_emergency_shutdown_blocks_every_future_authorization():
    guard = LiveCanaryGuard(_limits())
    assert guard.is_shutdown is False
    guard.emergency_shutdown("suspected runaway loop", by="user:1")
    assert guard.is_shutdown is True
    assert guard.kill_switch_engaged is True  # shutdown also engages the kill switch

    result = guard.authorize(_intent())
    assert result.allowed is False
    assert "EMERGENCY_SHUTDOWN" in result.reason


def test_emergency_shutdown_has_no_undo_method():
    """By design -- see LiveCanaryGuard.emergency_shutdown()'s docstring:
    resuming requires a fresh instance, not a flag flip."""
    guard = LiveCanaryGuard(_limits())
    assert not hasattr(guard, "resume") and not hasattr(guard, "un_shutdown")


# --------------------------------------------------------------------------- #
# 10: broker response validation (post-call)
# --------------------------------------------------------------------------- #
def test_check_10_accepts_a_well_formed_response():
    guard = LiveCanaryGuard(_limits())
    result = OrderResult(order_id="AO-1", symbol="X", side=OrderSide.BUY, quantity=1, status="OPEN")
    outcome = guard.validate_broker_response(result)
    assert outcome.passed is True


def test_check_10_rejects_a_none_response():
    guard = LiveCanaryGuard(_limits())
    outcome = guard.validate_broker_response(None)
    assert outcome.passed is False


def test_check_10_rejects_an_unrecognized_status():
    guard = LiveCanaryGuard(_limits())
    result = OrderResult(order_id="AO-1", symbol="X", side=OrderSide.BUY, quantity=1, status="WEIRD_STATUS")
    outcome = guard.validate_broker_response(result)
    assert outcome.passed is False


def test_check_10_rejects_a_non_rejected_order_with_no_order_id():
    guard = LiveCanaryGuard(_limits())
    result = OrderResult(order_id="", symbol="X", side=OrderSide.BUY, quantity=1, status="OPEN")
    outcome = guard.validate_broker_response(result)
    assert outcome.passed is False


def test_check_10_allows_a_rejected_order_with_no_order_id():
    guard = LiveCanaryGuard(_limits())
    result = OrderResult(order_id="", symbol="X", side=OrderSide.BUY, quantity=1, status="REJECTED", message="no funds")
    outcome = guard.validate_broker_response(result)
    assert outcome.passed is True


# --------------------------------------------------------------------------- #
# 11: position reconciliation (post-call)
# --------------------------------------------------------------------------- #
def test_check_11_reconciliation_passes_on_a_match():
    guard = LiveCanaryGuard(_limits())
    outcome = guard.reconcile_position("DoubleStraddelAlgo", "NIFTY", -65, -65)
    assert outcome.passed is True


def test_check_11_reconciliation_fails_and_alerts_on_a_mismatch():
    alerts = AlertManager()
    guard = LiveCanaryGuard(_limits(), alerts=alerts)
    outcome = guard.reconcile_position("DoubleStraddelAlgo", "NIFTY", -70, -65)
    assert outcome.passed is False
    unexpected = [a for a in alerts.alerts() if a.alert_type == "UNEXPECTED_POSITION"]
    assert len(unexpected) == 1
    assert unexpected[0].detail["quantity"] == -70
    assert unexpected[0].detail["expected"] == -65


# --------------------------------------------------------------------------- #
# Alerts raised on authorization failure
# --------------------------------------------------------------------------- #
def test_a_failed_authorization_raises_a_risk_breach_alert_prefixed_canary():
    alerts = AlertManager()
    guard = LiveCanaryGuard(_limits(max_order_quantity=1), alerts=alerts)
    guard.authorize(_intent(quantity=5))
    breach_alerts = [a for a in alerts.alerts() if a.alert_type == "RISK_BREACH"]
    assert len(breach_alerts) == 1
    assert breach_alerts[0].detail["check_name"] == "CANARY_MAX_ORDER_QUANTITY"
