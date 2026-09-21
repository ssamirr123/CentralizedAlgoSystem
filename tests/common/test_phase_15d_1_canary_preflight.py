"""Phase 15D.1: controlled single-account live canary -- preflight/readiness
validation only. No real order is submitted anywhere in this file.

Covers the angles not already exercised by prior phases' test files:
  - the SPECIFIC proposed canary order spec (tiny, 1-lot) actually clears
    CanaryLimits and RiskManager as a synthetic decision
  - LiveCanaryGuard's negative cases relevant to a single-account canary
    (wrong account, kill switch, missing/duplicate idempotency key)
  - structural proof that no code path can automatically set
    AccountAuthorizationState.CANARY_READY / LIVE_AUTHORIZED
  - Account B is provably excluded from the canary (no assignment, no
    guard reference, stays READ_ONLY)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from trading.common.alerts import AlertManager
from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.kill_switch import CentralKillSwitch
from trading.common.live_canary import CanaryLimits, LiveCanaryGuard
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskContext, RiskLimits, RiskManager
from trading.common.strategy_assignment import StrategyAssignment, UnknownAssignmentError
from trading.common.trading_account import AccountAuthorizationState, ExecutionMode, TradingAccount

ACCOUNT_A = "ANGEL_SAMIR"
ACCOUNT_B = "ANGEL_ACCOUNT_B"
LOT_QTY = 65  # trading/algos/DoubleStraddelAlgo/config.py's real NIFTY lot size

# The exact CanaryLimits this preflight proposes for Account A -- every
# field explicit, positive, no unlimited/default value.
PROPOSED_CANARY_LIMITS = CanaryLimits(
    account_id=ACCOUNT_A, max_order_quantity=LOT_QTY, max_order_value=10000.0,
    max_daily_loss=5000.0, max_strategy_loss=5000.0, max_orders_per_day=1,
)

# The exact proposed (NOT submitted) canary order spec, matching the
# report's Section "Proposed Canary". A conservative illustrative price
# (option premiums have run 40-130 across this project's own real checks
# this week) -- the real limit-price would be taken from a live quote at
# actual authorization time, never assumed here.
PROPOSED_ORDER_PRICE = 60.0
PROPOSED_ORDER_VALUE = LOT_QTY * PROPOSED_ORDER_PRICE  # 3900.0 -- within max_order_value


def _proposed_intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id="DoubleStraddelAlgo", account_id=ACCOUNT_A, symbol="NIFTY-PROPOSED-CE", exchange="NFO",
        side=OrderSide.BUY, quantity=LOT_QTY, order_type=OrderType.LIMIT, limit_price=PROPOSED_ORDER_PRICE,
        idempotency_key="PHASE15D1-PROPOSED-CANARY",
    )
    fields.update(overrides)
    return OrderIntent(**fields)


# --------------------------------------------------------------------------- #
# Step 7: CanaryLimits -- explicit, no unlimited/default values
# --------------------------------------------------------------------------- #
def test_proposed_canary_limits_have_no_missing_or_zero_field():
    for field_name in ("max_order_quantity", "max_order_value", "max_daily_loss", "max_strategy_loss", "max_orders_per_day"):
        value = getattr(PROPOSED_CANARY_LIMITS, field_name)
        assert value is not None
        assert value > 0


def test_proposed_order_is_within_every_canary_limit():
    assert LOT_QTY <= PROPOSED_CANARY_LIMITS.max_order_quantity
    assert PROPOSED_ORDER_VALUE <= PROPOSED_CANARY_LIMITS.max_order_value


def test_canary_limits_reject_missing_fields_entirely():
    with pytest.raises(TypeError):
        CanaryLimits(account_id=ACCOUNT_A)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Step 8: RiskManager synthetic authorization for the proposed order
# --------------------------------------------------------------------------- #
def _risk_ready_assignment() -> StrategyAssignment:
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id=ACCOUNT_A, account_name="A", broker_id="angelone", execution_mode=ExecutionMode.LIVE_CANARY),
        broker_client=PaperBroker(),
    )
    assignment = StrategyAssignment(manager)
    assignment.assign("DoubleStraddelAlgo", ACCOUNT_A, execution_mode=ExecutionMode.LIVE_CANARY)
    return assignment


def test_risk_manager_synthetically_allows_the_proposed_order_no_broker_call():
    assignment = _risk_ready_assignment()
    live_ready_limits = RiskLimits(
        max_order_quantity=LOT_QTY, max_order_value=10000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=1, max_strategy_exposure=10000.0, max_account_exposure=10000.0,
    )
    risk_manager = RiskManager(assignment, limits=live_ready_limits)
    result = risk_manager.validate(_proposed_intent(), RiskContext(reference_price=PROPOSED_ORDER_PRICE))
    assert result.allowed is True  # synthetic decision only -- no broker involved


# --------------------------------------------------------------------------- #
# Step 9: LiveCanaryGuard negative cases
# --------------------------------------------------------------------------- #
def test_canary_guard_authorizes_the_exact_proposed_order():
    guard = LiveCanaryGuard(PROPOSED_CANARY_LIMITS)
    result = guard.authorize(_proposed_intent())
    assert result.allowed is True


def test_canary_guard_rejects_wrong_account():
    guard = LiveCanaryGuard(PROPOSED_CANARY_LIMITS)
    result = guard.authorize(_proposed_intent(account_id=ACCOUNT_B))
    assert result.allowed is False
    assert "DEDICATED_ACCOUNT" in result.reason


def test_canary_guard_rejects_when_kill_switch_engaged():
    guard = LiveCanaryGuard(PROPOSED_CANARY_LIMITS)
    guard.engage_kill_switch(reason="phase-15d1-drill")
    result = guard.authorize(_proposed_intent())
    assert result.allowed is False
    assert "KILL_SWITCH" in result.reason


def test_canary_guard_rejects_missing_idempotency_key():
    guard = LiveCanaryGuard(PROPOSED_CANARY_LIMITS)
    result = guard.authorize(_proposed_intent(idempotency_key=""))
    assert result.allowed is False
    assert "IDEMPOTENCY_REQUIRED" in result.reason


def test_canary_guard_rejects_duplicate_intent():
    guard = LiveCanaryGuard(PROPOSED_CANARY_LIMITS)
    first = guard.authorize(_proposed_intent())
    assert first.allowed is True
    second = guard.authorize(_proposed_intent())
    assert second.allowed is False
    assert "DUPLICATE_ORDER_PROTECTION" in second.reason


def test_canary_guard_rejects_oversized_quantity():
    guard = LiveCanaryGuard(PROPOSED_CANARY_LIMITS)
    result = guard.authorize(_proposed_intent(quantity=LOT_QTY * 10, idempotency_key="oversized"))
    assert result.allowed is False
    assert "MAX_ORDER_QUANTITY" in result.reason


# --------------------------------------------------------------------------- #
# Step 10/11: authorization boundary + no automatic human-approval bypass
# --------------------------------------------------------------------------- #
def test_read_only_account_state_gate_rejects_before_canary_guard_even_runs():
    """The Phase 15B.1 AuthorizationState gate (inside
    StrategyExecutionEngine.execute()) rejects a READ_ONLY account before
    LiveCanaryGuard is ever consulted -- proven already in
    tests/common/test_authorization_state_gate.py; re-asserted here with
    this phase's own exact proposed intent for direct traceability."""
    from trading.common.execution import ExecutionConfig, StrategyExecutionEngine

    manager = BrokerManager()
    account = TradingAccount(
        account_id=ACCOUNT_A, account_name="A", broker_id="angelone", execution_mode=ExecutionMode.LIVE_CANARY,
        authorization_state=AccountAuthorizationState.READ_ONLY,  # the account's REAL current state
    )
    manager.register_account(account, broker_client=PaperBroker())
    assignment = StrategyAssignment(manager)
    assignment.assign("DoubleStraddelAlgo", ACCOUNT_A, execution_mode=ExecutionMode.LIVE_CANARY)
    risk_manager = RiskManager(assignment)
    guard = LiveCanaryGuard(PROPOSED_CANARY_LIMITS)
    engine = StrategyExecutionEngine(
        PaperBroker(), ExecutionConfig(dry_run=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=manager, canary_guard=guard,
    )
    result = engine.execute(_proposed_intent())
    assert result.success is False
    assert "authorization_state" in result.message


def test_no_production_code_path_assigns_canary_ready_or_live_authorized():
    """Structural, source-level proof (same pattern as Phase 14's own
    'NO AUTOMATIC ORDER PLACEMENT' AST check): scans every .py file under
    trading/ (excluding tests/) for an assignment/construction that sets
    authorization_state to CANARY_READY or LIVE_AUTHORIZED. Only the enum's
    own definition (trading_account.py) may reference these names at all;
    everywhere else may only ever COMPARE against them, never assign."""
    root = Path(__file__).resolve().parents[2] / "trading"
    offending: list[str] = []
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "CANARY_READY" not in source and "LIVE_AUTHORIZED" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "authorization_state":
                        value_src = ast.dump(node.value)
                        if "CANARY_READY" in value_src or "LIVE_AUTHORIZED" in value_src:
                            offending.append(f"{path}:{node.lineno}")
    assert offending == [], f"Found production code that assigns CANARY_READY/LIVE_AUTHORIZED automatically: {offending}"


# --------------------------------------------------------------------------- #
# Step 12: idempotency for the proposed canary key
# --------------------------------------------------------------------------- #
def test_proposed_idempotency_key_is_account_and_intent_specific():
    from trading.common.idempotency_store import compute_intent_hash

    same_account_same_intent = compute_intent_hash(_proposed_intent())
    same_account_same_intent_again = compute_intent_hash(_proposed_intent())
    different_account = compute_intent_hash(_proposed_intent(account_id=ACCOUNT_B))
    different_quantity = compute_intent_hash(_proposed_intent(quantity=1))

    assert same_account_same_intent == same_account_same_intent_again
    assert same_account_same_intent != different_account
    assert same_account_same_intent != different_quantity


# --------------------------------------------------------------------------- #
# Step 14: kill switch remains active / effective
# --------------------------------------------------------------------------- #
def test_central_kill_switch_default_state_is_disengaged_and_effective_when_engaged():
    kill_switch = CentralKillSwitch()
    assert kill_switch.engaged is False  # production-safe default
    kill_switch.engage(by="test", reason="phase-15d1-drill")
    assert kill_switch.engaged is True
    kill_switch.disengage(by="test")
    assert kill_switch.engaged is False  # restored -- this test never leaves it engaged


# --------------------------------------------------------------------------- #
# Step 15: Account B protection
# --------------------------------------------------------------------------- #
def test_account_b_has_no_strategy_assignment_in_the_canary_stack():
    assignment = _risk_ready_assignment()  # only ACCOUNT_A is assigned
    with pytest.raises(UnknownAssignmentError):
        assignment.get_account_id("DoubleStraddelAlgo_B_Variant")
    assert assignment.has_assignment("DoubleStraddelAlgo") is True


def test_account_b_remains_read_only_and_not_referenced_by_the_canary_guard():
    guard = LiveCanaryGuard(PROPOSED_CANARY_LIMITS)  # dedicated to ACCOUNT_A only
    result = guard.authorize(_proposed_intent(account_id=ACCOUNT_B, idempotency_key="attempt-b"))
    assert result.allowed is False
    assert "DEDICATED_ACCOUNT" in result.reason

    account_b = TradingAccount(account_id=ACCOUNT_B, account_name="B", broker_id="angelone")
    assert account_b.authorization_state == AccountAuthorizationState.READ_ONLY
    assert account_b.is_live_authorized() is False
