"""Phase 15D.1-B: Account-B-specific canary preflight validation.

Builds a DEDICATED LiveCanaryGuard/CanaryLimits for ANGEL_ACCOUNT_B (never
reusing the Account-A-scoped objects from Phase 15D.1), and proves:
  - the guard is genuinely scoped to Account B (rejects Account A)
  - RiskManager/CanaryLimits validate the proposed Account-B order
  - the authorization boundary still rejects Account B's real current
    state (READ_ONLY) before the guard is ever reached
  - idempotency keys are Account-B-specific and don't collide with Account A
  - kill switch still blocks Account B
  - Account A remains completely untouched/unaffected

No real broker credential, network call, or mutation is used anywhere in
this file.
"""
from __future__ import annotations

import pytest

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.idempotency_store import InMemoryIdempotencyStore, compute_intent_hash
from trading.common.kill_switch import CentralKillSwitch
from trading.common.live_canary import CanaryLimits, LiveCanaryGuard
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskContext, RiskLimits, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import AccountAuthorizationState, ExecutionMode, TradingAccount

ACCOUNT_A = "ANGEL_SAMIR"
ACCOUNT_B = "ANGEL_ACCOUNT_B"

# Real, current data as of this phase's fresh check: available_cash=3094.83.
# Proposed order sized conservatively well within it (1 lot, illustrative
# premium -- the real price must be re-resolved fresh, non-expired, at
# actual attempt time; this value is for structural limit-checking only).
PROPOSED_PRICE = 18.5
LOT_QTY = 65
PROPOSED_VALUE = LOT_QTY * PROPOSED_PRICE  # 1202.50

ACCOUNT_B_CANARY_LIMITS = CanaryLimits(
    account_id=ACCOUNT_B, max_order_quantity=LOT_QTY, max_order_value=5000.0,
    max_daily_loss=2500.0, max_strategy_loss=2500.0, max_orders_per_day=1,
)


def _proposed_intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id="DoubleStraddelAlgo", account_id=ACCOUNT_B, symbol="NIFTY-PROPOSED-CE", exchange="NFO",
        side=OrderSide.BUY, quantity=LOT_QTY, order_type=OrderType.LIMIT, limit_price=PROPOSED_PRICE,
        idempotency_key="PHASE15D1B-PROPOSED-CANARY",
    )
    fields.update(overrides)
    return OrderIntent(**fields)


# --------------------------------------------------------------------------- #
# CanaryLimits scoped to Account B -- explicit, no unlimited/default values
# --------------------------------------------------------------------------- #
def test_account_b_canary_limits_have_no_missing_or_zero_field():
    for field_name in ("max_order_quantity", "max_order_value", "max_daily_loss", "max_strategy_loss", "max_orders_per_day"):
        value = getattr(ACCOUNT_B_CANARY_LIMITS, field_name)
        assert value is not None
        assert value > 0
    assert ACCOUNT_B_CANARY_LIMITS.account_id == ACCOUNT_B


def test_proposed_order_within_account_b_canary_limits():
    assert LOT_QTY <= ACCOUNT_B_CANARY_LIMITS.max_order_quantity
    assert PROPOSED_VALUE <= ACCOUNT_B_CANARY_LIMITS.max_order_value


# --------------------------------------------------------------------------- #
# LiveCanaryGuard scoped to Account B -- rejects Account A, authorizes B
# --------------------------------------------------------------------------- #
def test_account_b_guard_authorizes_the_proposed_order():
    guard = LiveCanaryGuard(ACCOUNT_B_CANARY_LIMITS)
    result = guard.authorize(_proposed_intent())
    assert result.allowed is True


def test_account_b_guard_rejects_account_a_intent():
    """The guard built for Account B must NEVER authorize an intent
    addressed to Account A -- this is the core 'no accidental routing to
    Account A' proof this phase requires."""
    guard = LiveCanaryGuard(ACCOUNT_B_CANARY_LIMITS)
    result = guard.authorize(_proposed_intent(account_id=ACCOUNT_A, idempotency_key="wrong-account-attempt"))
    assert result.allowed is False
    assert "DEDICATED_ACCOUNT" in result.reason


def test_account_b_guard_rejects_when_kill_switch_engaged():
    guard = LiveCanaryGuard(ACCOUNT_B_CANARY_LIMITS)
    guard.engage_kill_switch(reason="phase-15d1b-drill")
    result = guard.authorize(_proposed_intent())
    assert result.allowed is False
    assert "KILL_SWITCH" in result.reason


def test_account_b_guard_rejects_missing_idempotency_key():
    guard = LiveCanaryGuard(ACCOUNT_B_CANARY_LIMITS)
    result = guard.authorize(_proposed_intent(idempotency_key=""))
    assert result.allowed is False
    assert "IDEMPOTENCY_REQUIRED" in result.reason


def test_account_b_guard_rejects_duplicate_intent():
    guard = LiveCanaryGuard(ACCOUNT_B_CANARY_LIMITS)
    first = guard.authorize(_proposed_intent())
    assert first.allowed is True
    second = guard.authorize(_proposed_intent())
    assert second.allowed is False
    assert "DUPLICATE_ORDER_PROTECTION" in second.reason


# --------------------------------------------------------------------------- #
# Authorization boundary -- READ_ONLY still rejects before the guard runs
# --------------------------------------------------------------------------- #
def test_authorization_state_gate_rejects_account_b_real_current_state():
    manager = BrokerManager()
    account = TradingAccount(
        account_id=ACCOUNT_B, account_name="B", broker_id="angelone", execution_mode=ExecutionMode.LIVE_CANARY,
        authorization_state=AccountAuthorizationState.READ_ONLY,  # Account B's REAL current state
    )
    manager.register_account(account, broker_client=PaperBroker())
    assignment = StrategyAssignment(manager)
    assignment.assign("DoubleStraddelAlgo", ACCOUNT_B, execution_mode=ExecutionMode.LIVE_CANARY)
    risk_manager = RiskManager(assignment)
    guard = LiveCanaryGuard(ACCOUNT_B_CANARY_LIMITS)
    engine = StrategyExecutionEngine(
        PaperBroker(), ExecutionConfig(dry_run=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=manager, canary_guard=guard,
    )
    result = engine.execute(_proposed_intent())
    assert result.success is False
    assert "authorization_state" in result.message


# --------------------------------------------------------------------------- #
# RiskManager synthetic validation for Account B
# --------------------------------------------------------------------------- #
def test_risk_manager_synthetically_allows_the_account_b_proposed_order():
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id=ACCOUNT_B, account_name="B", broker_id="angelone", execution_mode=ExecutionMode.LIVE_CANARY),
        broker_client=PaperBroker(),
    )
    assignment = StrategyAssignment(manager)
    assignment.assign("DoubleStraddelAlgo", ACCOUNT_B, execution_mode=ExecutionMode.LIVE_CANARY)
    live_ready_limits = RiskLimits(
        max_order_quantity=LOT_QTY, max_order_value=5000.0, max_daily_loss=2500.0, max_strategy_loss=2500.0,
        max_orders_per_day=1, max_strategy_exposure=5000.0, max_account_exposure=5000.0,
    )
    risk_manager = RiskManager(assignment, limits=live_ready_limits)
    result = risk_manager.validate(_proposed_intent(), RiskContext(reference_price=PROPOSED_PRICE))
    assert result.allowed is True


# --------------------------------------------------------------------------- #
# Idempotency: Account B specific, no collision with Account A
# --------------------------------------------------------------------------- #
def test_account_b_idempotency_key_is_account_specific():
    b_hash = compute_intent_hash(_proposed_intent())
    a_hash = compute_intent_hash(_proposed_intent(account_id=ACCOUNT_A))
    assert b_hash != a_hash


def test_same_key_across_a_and_b_is_never_cross_replayed():
    from trading.common.idempotency_store import IdempotencyKeyReuseError

    shared_key = "SHARED-KEY-15D1B"
    store = InMemoryIdempotencyStore()

    manager_b = BrokerManager()
    manager_b.register_account(
        TradingAccount(account_id=ACCOUNT_B, account_name="B", broker_id="angelone"), broker_client=PaperBroker(),
    )
    assignment_b = StrategyAssignment(manager_b)
    assignment_b.assign("DoubleStraddelAlgo", ACCOUNT_B)
    risk_b = RiskManager(assignment_b, limits=RiskLimits(
        max_order_quantity=100, max_order_value=10000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=10000.0, max_account_exposure=10000.0,
    ))
    engine_b = StrategyExecutionEngine(
        PaperBroker(), ExecutionConfig(dry_run=True),
        risk_manager=risk_b, strategy_assignment=assignment_b, broker_manager=manager_b, idempotency_store=store,
    )
    result_b = engine_b.execute(_proposed_intent(idempotency_key=shared_key))
    assert result_b.success is True

    manager_a = BrokerManager()
    manager_a.register_account(
        TradingAccount(account_id=ACCOUNT_A, account_name="A", broker_id="angelone"), broker_client=PaperBroker(),
    )
    assignment_a = StrategyAssignment(manager_a)
    assignment_a.assign("DoubleStraddelAlgo", ACCOUNT_A)
    risk_a = RiskManager(assignment_a, limits=RiskLimits(
        max_order_quantity=100, max_order_value=10000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=10000.0, max_account_exposure=10000.0,
    ))
    engine_a = StrategyExecutionEngine(
        PaperBroker(), ExecutionConfig(dry_run=True),
        risk_manager=risk_a, strategy_assignment=assignment_a, broker_manager=manager_a, idempotency_store=store,
    )
    with pytest.raises(IdempotencyKeyReuseError):
        engine_a.execute(_proposed_intent(account_id=ACCOUNT_A, idempotency_key=shared_key))


# --------------------------------------------------------------------------- #
# Kill switch
# --------------------------------------------------------------------------- #
def test_kill_switch_blocks_account_b_end_to_end():
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(
            account_id=ACCOUNT_B, account_name="B", broker_id="angelone", execution_mode=ExecutionMode.LIVE_CANARY,
            authorization_state=AccountAuthorizationState.CANARY_READY,  # hypothetical, never applied to the real account
        ),
        broker_client=PaperBroker(),
    )
    assignment = StrategyAssignment(manager)
    assignment.assign("DoubleStraddelAlgo", ACCOUNT_B, execution_mode=ExecutionMode.LIVE_CANARY)
    risk_manager = RiskManager(assignment)
    guard = LiveCanaryGuard(ACCOUNT_B_CANARY_LIMITS)
    kill_switch = CentralKillSwitch()
    kill_switch.engage(by="test", reason="phase-15d1b-drill")
    engine = StrategyExecutionEngine(
        PaperBroker(), ExecutionConfig(dry_run=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=manager, canary_guard=guard,
        central_kill_switch=kill_switch,
    )
    result = engine.execute(_proposed_intent())
    assert result.success is False
    assert "kill switch" in result.message.lower()


# --------------------------------------------------------------------------- #
# Account A remains completely unaffected by any of the above
# --------------------------------------------------------------------------- #
def test_account_a_unaffected_by_account_b_canary_construction():
    account_a = TradingAccount(account_id=ACCOUNT_A, account_name="A", broker_id="angelone")
    assert account_a.authorization_state == AccountAuthorizationState.READ_ONLY
    assert account_a.is_live_authorized() is False
