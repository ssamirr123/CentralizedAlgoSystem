"""trading/common/risk_manager.py -- the centralized pre-trade interception
point. Covers all 14 named checks, the fail-closed guarantee, and the full
audit-trail (RiskCheckResult.checks) contract."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskContext, RiskLimits, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import ExecutionMode, TradingAccount


def _assignment_with(account_id="PAPER_MAIN", enabled=True) -> StrategyAssignment:
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id=account_id, account_name="Paper", broker_id="paper", enabled=enabled),
        broker_client=PaperBroker(),
    )
    assignment = StrategyAssignment(manager)
    if enabled:
        assignment.assign("DoubleStraddelAlgo", account_id)
    return assignment


def _intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id="DoubleStraddelAlgo",
        account_id="PAPER_MAIN",
        symbol="NIFTY24950CE",
        exchange="NFO",
        side=OrderSide.SELL,
        quantity=50,
        order_type=OrderType.LIMIT,
        limit_price=125.5,
    )
    fields.update(overrides)
    return OrderIntent(**fields)


def test_valid_intent_is_allowed():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent())
    assert result.allowed is True
    assert result.reason == ""


def test_non_positive_quantity_is_denied():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent(quantity=0))
    assert result.allowed is False
    assert "quantity" in result.reason


def test_unknown_strategy_assignment_is_denied():
    assignment = _assignment_with()  # only "DoubleStraddelAlgo" is assigned
    risk_manager = RiskManager(assignment)
    result = risk_manager.validate(_intent(strategy_id="SomeOtherAlgo"))
    assert result.allowed is False
    assert "assignment" in result.reason.lower()


def test_disabled_account_is_denied():
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id="PAPER_MAIN", account_name="Paper", broker_id="paper", enabled=True),
        broker_client=PaperBroker(),
    )
    assignment = StrategyAssignment(manager)
    assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN")
    risk_manager = RiskManager(assignment)

    manager.get_account("PAPER_MAIN").enabled = False

    result = risk_manager.validate(_intent())
    assert result.allowed is False
    assert "disabled" in result.reason.lower()


def test_limit_order_requires_limit_price():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent(limit_price=None))
    assert result.allowed is False
    assert "limit_price" in result.reason


def test_market_order_does_not_require_limit_price():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent(order_type=OrderType.MARKET, limit_price=None))
    assert result.allowed is True


def test_disabled_strategy_context_is_denied():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent(), RiskContext(strategy_enabled=False))
    assert result.allowed is False
    assert "disabled" in result.reason.lower()


def _outcome(result, name):
    return next(c for c in result.checks if c.name == name)


# --------------------------------------------------------------------------- #
# Explicit APPROVED/REJECTED status + full audit trail
# --------------------------------------------------------------------------- #
def test_approved_result_has_explicit_status_and_full_audit_trail():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent())

    assert result.status == "APPROVED"
    assert result.allowed is True
    # All 15 named checks are always recorded, not just failures. (Phase
    # 14.6 Blocker E added MAX_ORDERS_PER_DAY as a 15th check -- this test
    # originally pinned 14; updated deliberately, not weakened.)
    names = {c.name for c in result.checks}
    assert names == {
        "STRATEGY_ENABLED", "ACCOUNT_ENABLED", "EXECUTION_MODE_ALLOWED", "MAX_ORDER_QUANTITY",
        "MAX_POSITION_QUANTITY", "MAX_STRATEGY_EXPOSURE", "MAX_ACCOUNT_EXPOSURE", "MAX_DAILY_LOSS",
        "MAX_STRATEGY_LOSS", "DUPLICATE_ORDER_PROTECTION", "MARKET_SESSION_VALIDATION", "KILL_SWITCH",
        "INSTRUMENT_VALIDATION", "ORDER_VALUE_LIMIT", "MAX_ORDERS_PER_DAY",
    }
    assert all(c.passed for c in result.checks)


def test_rejected_result_has_explicit_status():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent(quantity=0))
    assert result.status == "REJECTED"


def test_every_decision_carries_timestamp_strategy_account_correlation_id():
    risk_manager = RiskManager(_assignment_with())
    intent = _intent()
    result = risk_manager.validate(intent)

    assert result.timestamp
    datetime.fromisoformat(result.timestamp)  # must be a real ISO timestamp
    assert result.strategy_id == intent.strategy_id
    assert result.account_id == intent.account_id
    assert result.correlation_id == intent.correlation_id


def test_rejection_still_carries_every_check_not_just_the_failure():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent(quantity=0))

    assert len(result.checks) == 15  # Phase 14.6 added MAX_ORDERS_PER_DAY as a 15th check
    assert _outcome(result, "MAX_ORDER_QUANTITY").passed is False
    assert _outcome(result, "STRATEGY_ENABLED").passed is True  # unaffected checks still recorded as passing


# --------------------------------------------------------------------------- #
# 3: Execution mode allowed
# --------------------------------------------------------------------------- #
def _assignment_with_mode(mode: ExecutionMode) -> StrategyAssignment:
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id="PAPER_MAIN", account_name="Paper", broker_id="paper", execution_mode=mode),
        broker_client=PaperBroker(),
    )
    assignment = StrategyAssignment(manager)
    assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN")
    return assignment


def test_execution_mode_allowed_by_default_when_unrestricted():
    risk_manager = RiskManager(_assignment_with_mode(ExecutionMode.LIVE))
    result = risk_manager.validate(_intent())
    assert _outcome(result, "EXECUTION_MODE_ALLOWED").passed is True


def test_execution_mode_rejected_when_not_in_allowed_set():
    risk_manager = RiskManager(_assignment_with_mode(ExecutionMode.LIVE))
    context = RiskContext(allowed_execution_modes=frozenset({ExecutionMode.SHADOW, ExecutionMode.PAPER}))

    result = risk_manager.validate(_intent(), context)

    assert result.allowed is False
    assert "EXECUTION_MODE_ALLOWED" in result.reason


def test_execution_mode_approved_when_in_allowed_set():
    risk_manager = RiskManager(_assignment_with_mode(ExecutionMode.SHADOW))
    context = RiskContext(allowed_execution_modes=frozenset({ExecutionMode.SHADOW}))

    result = risk_manager.validate(_intent(), context)

    assert result.allowed is True


# --------------------------------------------------------------------------- #
# 4: Maximum order quantity
# --------------------------------------------------------------------------- #
def test_max_order_quantity_rejects_over_limit():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_order_quantity=50))
    result = risk_manager.validate(_intent(quantity=51))
    assert result.allowed is False
    assert "MAX_ORDER_QUANTITY" in result.reason


def test_max_order_quantity_allows_at_exactly_the_limit():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_order_quantity=50))
    result = risk_manager.validate(_intent(quantity=50))
    assert result.allowed is True


def test_max_order_quantity_unconfigured_never_rejects():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent(quantity=100_000))
    assert _outcome(result, "MAX_ORDER_QUANTITY").passed is True


# --------------------------------------------------------------------------- #
# 5: Maximum position quantity
# --------------------------------------------------------------------------- #
def test_max_position_quantity_rejects_when_resulting_position_too_large():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_position_quantity=100))
    context = RiskContext(current_position_quantity=-80)  # already short 80

    result = risk_manager.validate(_intent(side=OrderSide.SELL, quantity=50), context)  # would go to -130

    assert result.allowed is False
    assert "MAX_POSITION_QUANTITY" in result.reason


def test_max_position_quantity_allows_when_reducing_the_position():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_position_quantity=100))
    context = RiskContext(current_position_quantity=-80)

    result = risk_manager.validate(_intent(side=OrderSide.BUY, quantity=50), context)  # would go to -30

    assert result.allowed is True


# --------------------------------------------------------------------------- #
# 6 + 7: Maximum strategy / account exposure
# --------------------------------------------------------------------------- #
def test_max_strategy_exposure_rejects_over_limit():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_strategy_exposure=5000.0))
    context = RiskContext(strategy_exposure=4900.0)

    result = risk_manager.validate(_intent(quantity=50, limit_price=125.5), context)  # +6275

    assert result.allowed is False
    assert "MAX_STRATEGY_EXPOSURE" in result.reason


def test_max_account_exposure_rejects_over_limit():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_account_exposure=5000.0))
    context = RiskContext(account_exposure=4900.0)

    result = risk_manager.validate(_intent(quantity=50, limit_price=125.5), context)

    assert result.allowed is False
    assert "MAX_ACCOUNT_EXPOSURE" in result.reason


def test_exposure_limits_allow_when_within_bounds():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_strategy_exposure=100_000.0, max_account_exposure=100_000.0))
    context = RiskContext(strategy_exposure=100.0, account_exposure=100.0)

    result = risk_manager.validate(_intent(quantity=50, limit_price=125.5), context)

    assert result.allowed is True


# --------------------------------------------------------------------------- #
# 8 + 9: Maximum daily / strategy loss
# --------------------------------------------------------------------------- #
def test_max_daily_loss_rejects_when_breached():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_daily_loss=20_000.0))
    context = RiskContext(daily_pnl=-20_500.0)

    result = risk_manager.validate(_intent(), context)

    assert result.allowed is False
    assert "MAX_DAILY_LOSS" in result.reason


def test_max_daily_loss_allows_when_within_bounds():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_daily_loss=20_000.0))
    context = RiskContext(daily_pnl=-5_000.0)

    result = risk_manager.validate(_intent(), context)

    assert result.allowed is True


def test_max_strategy_loss_rejects_when_breached():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_strategy_loss=10_000.0))
    context = RiskContext(strategy_pnl=-10_001.0)

    result = risk_manager.validate(_intent(), context)

    assert result.allowed is False
    assert "MAX_STRATEGY_LOSS" in result.reason


def test_daily_loss_check_ignores_positive_pnl():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_daily_loss=20_000.0))
    context = RiskContext(daily_pnl=50_000.0)  # profit, not loss
    result = risk_manager.validate(_intent(), context)
    assert result.allowed is True


# --------------------------------------------------------------------------- #
# 10: Duplicate order protection
# --------------------------------------------------------------------------- #
def test_duplicate_idempotency_key_is_rejected_on_second_use():
    risk_manager = RiskManager(_assignment_with())
    intent = _intent(idempotency_key="hedge-entry-ce-2026-09-15")

    first = risk_manager.validate(intent)
    second = risk_manager.validate(intent)

    assert first.allowed is True
    assert second.allowed is False
    assert "DUPLICATE_ORDER_PROTECTION" in second.reason


def test_distinct_idempotency_keys_are_both_approved():
    risk_manager = RiskManager(_assignment_with())
    first = risk_manager.validate(_intent(idempotency_key="entry-1"))
    second = risk_manager.validate(_intent(idempotency_key="entry-2"))
    assert first.allowed is True
    assert second.allowed is True


def test_empty_idempotency_key_never_triggers_duplicate_protection():
    risk_manager = RiskManager(_assignment_with())
    first = risk_manager.validate(_intent(idempotency_key=""))
    second = risk_manager.validate(_intent(idempotency_key=""))
    assert first.allowed is True
    assert second.allowed is True


# --------------------------------------------------------------------------- #
# 11: Market/session validation
# --------------------------------------------------------------------------- #
def test_market_session_not_enforced_by_default():
    risk_manager = RiskManager(_assignment_with())
    weekend = datetime(2026, 9, 13, 3, 0, tzinfo=timezone.utc)  # a Sunday, outside any session
    result = risk_manager.validate(_intent(), RiskContext(now=weekend))
    assert _outcome(result, "MARKET_SESSION_VALIDATION").passed is True


def test_market_session_rejects_a_weekend_when_enforced():
    risk_manager = RiskManager(_assignment_with())
    sunday = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)
    result = risk_manager.validate(_intent(), RiskContext(enforce_market_hours=True, now=sunday))
    assert result.allowed is False
    assert "MARKET_SESSION_VALIDATION" in result.reason


def test_market_session_rejects_outside_session_hours_when_enforced():
    risk_manager = RiskManager(_assignment_with())
    late_night = datetime(2026, 9, 14, 22, 0, tzinfo=timezone.utc)  # a Monday, 22:00
    result = risk_manager.validate(_intent(), RiskContext(enforce_market_hours=True, now=late_night))
    assert result.allowed is False


def test_market_session_approves_a_weekday_within_hours_when_enforced():
    risk_manager = RiskManager(_assignment_with())
    weekday_midday = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)  # a Monday, 11:00
    result = risk_manager.validate(_intent(), RiskContext(enforce_market_hours=True, now=weekday_midday))
    assert result.allowed is True


# --------------------------------------------------------------------------- #
# 12: Kill switch
# --------------------------------------------------------------------------- #
def test_kill_switch_rejects_unconditionally():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent(), RiskContext(kill_switch_engaged=True))
    assert result.allowed is False
    assert "KILL_SWITCH" in result.reason


def test_kill_switch_off_by_default():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent())
    assert _outcome(result, "KILL_SWITCH").passed is True


# --------------------------------------------------------------------------- #
# 13: Instrument validation
# --------------------------------------------------------------------------- #
def test_instrument_validation_rejects_empty_symbol():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent(symbol=""))
    assert result.allowed is False
    assert "INSTRUMENT_VALIDATION" in result.reason


def test_instrument_validation_rejects_unrecognized_exchange():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent(exchange="MOONX"))
    assert result.allowed is False


def test_instrument_validation_accepts_known_exchanges():
    risk_manager = RiskManager(_assignment_with())
    for exchange in ("NFO", "NSE", "BSE"):
        result = risk_manager.validate(_intent(exchange=exchange))
        assert result.allowed is True, exchange


# --------------------------------------------------------------------------- #
# 14: Order value limit
# --------------------------------------------------------------------------- #
def test_order_value_limit_rejects_over_limit():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_order_value=5000.0))
    result = risk_manager.validate(_intent(quantity=50, limit_price=125.5))  # value = 6275
    assert result.allowed is False
    assert "ORDER_VALUE_LIMIT" in result.reason


def test_order_value_limit_allows_within_limit():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_order_value=10_000.0))
    result = risk_manager.validate(_intent(quantity=50, limit_price=125.5))
    assert result.allowed is True


def test_order_value_limit_uses_reference_price_for_market_orders():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_order_value=1000.0))
    intent = _intent(order_type=OrderType.MARKET, limit_price=None, quantity=50)
    context = RiskContext(reference_price=100.0)  # value = 5000, over the limit

    result = risk_manager.validate(intent, context)

    assert result.allowed is False
    assert "ORDER_VALUE_LIMIT" in result.reason


def test_order_value_limit_rejects_when_no_price_is_available():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_order_value=1000.0))
    intent = _intent(order_type=OrderType.MARKET, limit_price=None)  # no reference_price supplied either
    result = risk_manager.validate(intent)
    assert result.allowed is False
    assert "ORDER_VALUE_LIMIT" in result.reason


# --------------------------------------------------------------------------- #
# Fail-closed: unexpected internal errors reject, never silently approve
# --------------------------------------------------------------------------- #
def test_internal_error_during_evaluation_fails_closed():
    risk_manager = RiskManager(_assignment_with())

    def _boom(*_a, **_k):
        raise RuntimeError("simulated internal failure")

    risk_manager._check_max_order_quantity = _boom  # type: ignore[method-assign]

    result = risk_manager.validate(_intent())

    assert result.allowed is False
    assert result.status == "REJECTED"
    assert any(c.name == "INTERNAL_ERROR" for c in result.checks)


def test_risk_manager_is_not_hardcoded_to_any_broker():
    """Structural guard: RiskManager must never import a broker SDK."""
    import inspect

    import trading.common.risk_manager as module

    source = inspect.getsource(module)
    for forbidden in ("SmartApi", "SmartConnect", "breeze_connect", "angelone", "AngelOne"):
        assert forbidden not in source


# --------------------------------------------------------------------------- #
# Phase 14.6 Blocker E: RiskLimits.is_live_ready()
# --------------------------------------------------------------------------- #
def test_default_risk_limits_are_not_live_ready():
    ready, reason = RiskLimits().is_live_ready()
    assert ready is False
    assert "max_order_quantity" in reason


def test_fully_configured_positive_limits_are_live_ready():
    ready, reason = RiskLimits(
        max_order_quantity=5, max_order_value=1000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=10000.0, max_account_exposure=20000.0,
    ).is_live_ready()
    assert ready is True
    assert reason == ""


def test_partial_configuration_is_not_live_ready():
    ready, reason = RiskLimits(max_order_quantity=5, max_order_value=1000.0).is_live_ready()
    assert ready is False
    assert "max_daily_loss" in reason
    assert "max_orders_per_day" in reason


@pytest.mark.parametrize("bad_value", [0, -1])
def test_zero_or_negative_limits_are_not_treated_as_unlimited(bad_value):
    ready, reason = RiskLimits(
        max_order_quantity=bad_value, max_order_value=1000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=10000.0, max_account_exposure=20000.0,
    ).is_live_ready()
    assert ready is False
    assert "max_order_quantity" in reason


def test_only_one_exposure_field_set_is_still_not_live_ready():
    """Both max_strategy_exposure AND max_account_exposure are required --
    setting only one would leave the other silently unenforced for LIVE."""
    ready, reason = RiskLimits(
        max_order_quantity=5, max_order_value=1000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=10000.0,  # account_exposure left unset
    ).is_live_ready()
    assert ready is False
    assert "max_account_exposure" in reason


def test_malformed_non_numeric_value_fails_closed_not_crash():
    """Phase 14.7 regression: RiskLimits is a plain dataclass and does not
    enforce field types at runtime -- a malformed (non-numeric) value used
    to raise TypeError out of is_live_ready() uncaught, which would have
    propagated out of StrategyExecutionEngine.execute() as an unhandled
    crash instead of a clean rejection. Fixed to treat a malformed value
    identically to a missing one: fail closed, never raise."""
    ready, reason = RiskLimits(
        max_order_quantity="not-a-number", max_order_value=1000.0, max_daily_loss=5000.0,
        max_strategy_loss=5000.0, max_orders_per_day=10, max_strategy_exposure=10000.0,
        max_account_exposure=20000.0,
    ).is_live_ready()
    assert ready is False
    assert "max_order_quantity" in reason


# --------------------------------------------------------------------------- #
# Phase 14.6: 15th check, MAX_ORDERS_PER_DAY
# --------------------------------------------------------------------------- #
def test_max_orders_per_day_unconfigured_always_passes():
    risk_manager = RiskManager(_assignment_with())
    for _ in range(5):
        result = risk_manager.validate(_intent())
        assert _outcome(result, "MAX_ORDERS_PER_DAY").passed is True


def test_max_orders_per_day_blocks_once_limit_reached():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_orders_per_day=2))
    r1 = risk_manager.validate(_intent())
    r2 = risk_manager.validate(_intent())
    r3 = risk_manager.validate(_intent())

    assert r1.allowed is True
    assert r2.allowed is True
    assert r3.allowed is False
    assert _outcome(r3, "MAX_ORDERS_PER_DAY").passed is False


def test_max_orders_per_day_rejected_intent_does_not_consume_budget():
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_orders_per_day=1, max_order_quantity=10))
    # Rejected for an unrelated reason (quantity) -- must not consume the
    # day's one-order budget.
    risk_manager.validate(_intent(quantity=999))
    result = risk_manager.validate(_intent(quantity=1))
    assert result.allowed is True


def test_max_orders_per_day_is_scoped_per_strategy():
    """A different strategy_id has its own, independent daily budget --
    checked directly against the MAX_ORDERS_PER_DAY outcome (not overall
    `allowed`, since "OtherStrategy" has no account assignment of its own
    in this fixture and would fail ACCOUNT_ENABLED for an unrelated
    reason -- that's not what this test is about)."""
    risk_manager = RiskManager(_assignment_with(), RiskLimits(max_orders_per_day=1))
    r1 = risk_manager.validate(_intent())
    other_intent = _intent()
    object.__setattr__(other_intent, "strategy_id", "OtherStrategy")
    r2 = risk_manager.validate(other_intent)
    assert r1.allowed is True
    assert _outcome(r2, "MAX_ORDERS_PER_DAY").passed is True  # different strategy, independent budget
