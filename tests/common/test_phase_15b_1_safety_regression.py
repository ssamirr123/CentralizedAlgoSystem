"""Phase 15B.1 Step 6/7: additional edge cases and safety-regression proofs.

Proves the new AuthorizationState gate:
  (a) fails closed on a missing account / an invalid authorization_state,
  (b) isolates two accounts' authorization states from each other,
  (c) never substitutes for RiskManager, LiveCanaryGuard, CentralKillSwitch,
      or idempotency -- passing this gate is necessary, never sufficient.

No real broker call is made anywhere in this file (PaperBroker/dry_run only).
"""
from __future__ import annotations

import pytest

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.idempotency_store import InMemoryIdempotencyStore
from trading.common.kill_switch import CentralKillSwitch
from trading.common.live_canary import CanaryLimits, LiveCanaryGuard
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskLimits, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import AccountAuthorizationState, ExecutionMode, TradingAccount

_LIVE_READY_LIMITS = RiskLimits(
    max_order_quantity=100, max_order_value=100000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
    max_orders_per_day=10, max_strategy_exposure=100000.0, max_account_exposure=100000.0,
)


def _intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id="StrategyA", account_id="ACC1", symbol="NIFTY24950CE", exchange="NFO",
        side=OrderSide.SELL, quantity=5, order_type=OrderType.LIMIT, limit_price=100.0,
        idempotency_key="idem-1",
    )
    fields.update(overrides)
    return OrderIntent(**fields)


def _build(account, *, risk_limits=None, canary_guard=None, central_kill_switch=None, idempotency_store=None):
    manager = BrokerManager()
    broker = PaperBroker()
    manager.register_account(account, broker_client=broker)
    assignment = StrategyAssignment(manager)
    assignment.assign(account.metadata.get("strategy_id", "StrategyA"), account.account_id)
    risk_manager = RiskManager(assignment, limits=risk_limits or RiskLimits())
    engine = StrategyExecutionEngine(
        broker, ExecutionConfig(dry_run=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=manager,
        canary_guard=canary_guard, central_kill_switch=central_kill_switch, idempotency_store=idempotency_store,
    )
    return engine, manager


# --------------------------------------------------------------------------- #
# Fail-closed edge cases
# --------------------------------------------------------------------------- #
def test_missing_account_fails_closed_not_a_crash():
    manager = BrokerManager()
    assignment = StrategyAssignment(manager)
    risk_manager = RiskManager(assignment)
    engine = StrategyExecutionEngine(
        PaperBroker(), ExecutionConfig(dry_run=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=manager,
    )
    result = engine.execute(_intent(strategy_id="NeverAssigned"))
    assert result.success is False


def test_invalid_authorization_state_on_the_instance_fails_closed():
    """TradingAccount is a mutable dataclass -- __post_init__ validates at
    CONSTRUCTION time, but nothing stops a later direct attribute
    assignment from putting a non-enum value on the instance. The gate must
    not trust that value; it must fail closed rather than crash or silently
    pass."""
    account = TradingAccount(account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.PAPER)
    account.authorization_state = "NOT_A_REAL_STATE"  # bypasses __post_init__ entirely
    engine, _ = _build(account)
    result = engine.execute(_intent())
    assert result.success is False


# --------------------------------------------------------------------------- #
# Account A / Account B isolation
# --------------------------------------------------------------------------- #
def test_account_a_live_authorized_account_b_read_only_are_isolated():
    manager = BrokerManager()
    account_a = TradingAccount(
        account_id="ACCOUNT_A", account_name="A", broker_id="paper", execution_mode=ExecutionMode.LIVE,
        authorization_state=AccountAuthorizationState.LIVE_AUTHORIZED,
    )
    account_b = TradingAccount(
        account_id="ACCOUNT_B", account_name="B", broker_id="paper", execution_mode=ExecutionMode.LIVE,
        authorization_state=AccountAuthorizationState.READ_ONLY,
    )
    manager.register_account(account_a, broker_client=PaperBroker())
    manager.register_account(account_b, broker_client=PaperBroker())
    assignment = StrategyAssignment(manager)
    assignment.assign("StrategyA", "ACCOUNT_A")
    assignment.assign("StrategyB", "ACCOUNT_B")
    risk_manager = RiskManager(assignment, limits=_LIVE_READY_LIMITS)
    engine = StrategyExecutionEngine(
        PaperBroker(), ExecutionConfig(dry_run=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=manager,
    )

    result_a = engine.execute(_intent(strategy_id="StrategyA", account_id="ACCOUNT_A", idempotency_key="a-1"))
    result_b = engine.execute(_intent(strategy_id="StrategyB", account_id="ACCOUNT_B", idempotency_key="b-1"))

    assert result_a.success is True
    assert result_b.success is False
    # Account B's rejection must never be influenced by Account A's state.
    assert "ACCOUNT_B" in result_b.message


# --------------------------------------------------------------------------- #
# Step 7: authorization PASS is NEVER sufficient on its own
# --------------------------------------------------------------------------- #
def test_authorization_pass_plus_risk_fail_is_still_rejected():
    account = TradingAccount(
        account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.LIVE,
        authorization_state=AccountAuthorizationState.LIVE_AUTHORIZED,
    )
    tight_limits = RiskLimits(
        max_order_quantity=1, max_order_value=100000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=100000.0, max_account_exposure=100000.0,
    )
    engine, _ = _build(account, risk_limits=tight_limits)
    result = engine.execute(_intent(quantity=50))
    assert result.success is False
    assert "MAX_ORDER_QUANTITY" in result.message


def test_authorization_pass_plus_kill_switch_active_is_still_rejected():
    account = TradingAccount(
        account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.LIVE,
        authorization_state=AccountAuthorizationState.LIVE_AUTHORIZED,
    )
    kill_switch = CentralKillSwitch()
    kill_switch.engage(by="test", reason="drill")
    engine, _ = _build(account, risk_limits=_LIVE_READY_LIMITS, central_kill_switch=kill_switch)
    result = engine.execute(_intent())
    assert result.success is False
    assert "kill switch" in result.message.lower()


def test_authorization_pass_plus_live_canary_guard_fail_is_still_rejected():
    account = TradingAccount(
        account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.LIVE_CANARY,
        authorization_state=AccountAuthorizationState.CANARY_READY,
    )
    # A guard whose own limits reject this exact intent (quantity too large).
    guard = LiveCanaryGuard(CanaryLimits(
        account_id="ACC1", max_order_quantity=1, max_order_value=100000.0,
        max_daily_loss=5000.0, max_strategy_loss=5000.0, max_orders_per_day=10,
    ))
    engine, _ = _build(account, canary_guard=guard)
    result = engine.execute(_intent(quantity=50))
    assert result.success is False


def test_authorization_pass_plus_idempotency_key_reuse_mismatch_is_never_silently_replayed():
    from trading.common.idempotency_store import IdempotencyKeyReuseError

    account = TradingAccount(
        account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.PAPER,
    )
    store = InMemoryIdempotencyStore()
    engine, _ = _build(account, idempotency_store=store)

    first = engine.execute(_intent(idempotency_key="shared-key", quantity=5))
    assert first.success is True

    # Same key, materially different intent (different quantity) -- must
    # raise, never silently replay the first result or place a new order.
    with pytest.raises(IdempotencyKeyReuseError):
        engine.execute(_intent(idempotency_key="shared-key", quantity=999))


def test_authorization_gate_does_not_bypass_idempotent_replay_short_circuit():
    """A second execute() call with the SAME key and SAME intent content
    must replay the cached result rather than re-authorizing/re-risk-checking
    from scratch -- proving the new gate sits logically alongside, not in
    front of, the existing idempotency short-circuit for genuine replays."""
    account = TradingAccount(account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.PAPER)
    store = InMemoryIdempotencyStore()
    engine, _ = _build(account, idempotency_store=store)

    first = engine.execute(_intent(idempotency_key="replay-key"))
    second = engine.execute(_intent(idempotency_key="replay-key"))
    assert first.success is True
    assert second.order_id == first.order_id
