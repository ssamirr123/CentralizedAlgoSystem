"""Phase 15B.1: the AuthorizationState hard gate inside StrategyExecutionEngine.execute().

Pipeline required: Strategy -> OrderIntent -> StrategyExecutionEngine ->
AuthorizationState Gate -> RiskManager -> ExecutionMode/LiveCanaryGuard ->
Idempotency -> Broker.

This gate is NEW and ADDITIONAL: it never replaces RiskManager, LiveCanaryGuard,
CentralKillSwitch, or RiskLimits.is_live_ready() -- it runs strictly before
RiskManager and can reject an intent none of those gates would have seen yet.
"""
from __future__ import annotations

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.live_canary import CanaryLimits, LiveCanaryGuard
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskLimits, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import AccountAuthorizationState, ExecutionMode, TradingAccount


def _intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id="StrategyA", account_id="ACC1", symbol="NIFTY24950CE", exchange="NFO",
        side=OrderSide.SELL, quantity=5, order_type=OrderType.LIMIT, limit_price=100.0,
        idempotency_key="idem-1",
    )
    fields.update(overrides)
    return OrderIntent(**fields)


def _engine_for(
    account: TradingAccount,
    *,
    risk_limits: RiskLimits | None = None,
    canary_guard: LiveCanaryGuard | None = None,
) -> StrategyExecutionEngine:
    manager = BrokerManager()
    broker = PaperBroker()
    manager.register_account(account, broker_client=broker)
    assignment = StrategyAssignment(manager)
    assignment.assign("StrategyA", account.account_id)
    risk_manager = RiskManager(assignment, limits=risk_limits or RiskLimits())
    return StrategyExecutionEngine(
        broker, ExecutionConfig(dry_run=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=manager,
        canary_guard=canary_guard,
    )


# --------------------------------------------------------------------------- #
# PAPER/SHADOW: READ_ONLY (the default) must remain sufficient
# --------------------------------------------------------------------------- #
def test_paper_account_with_default_read_only_state_still_executes():
    account = TradingAccount(account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.PAPER)
    assert account.authorization_state == AccountAuthorizationState.READ_ONLY
    engine = _engine_for(account)
    result = engine.execute(_intent())
    assert result.success is True


def test_shadow_account_with_default_read_only_state_still_executes():
    account = TradingAccount(account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.SHADOW)
    engine = _engine_for(account)
    result = engine.execute(_intent(idempotency_key="idem-shadow"))
    assert result.success is True


# --------------------------------------------------------------------------- #
# DISABLED / KILLED: blocked in every mode, even PAPER
# --------------------------------------------------------------------------- #
def test_disabled_authorization_state_blocks_paper_execution():
    account = TradingAccount(
        account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.PAPER,
        authorization_state=AccountAuthorizationState.DISABLED,
    )
    engine = _engine_for(account)
    result = engine.execute(_intent())
    assert result.success is False
    assert "authorization_state" in result.message


def test_killed_account_blocks_paper_execution_and_is_irreversible():
    account = TradingAccount(account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.PAPER)
    account.set_killed(reason="test")
    engine = _engine_for(account)
    result = engine.execute(_intent())
    assert result.success is False
    assert "KILLED" in result.message


# --------------------------------------------------------------------------- #
# LIVE: requires exactly LIVE_AUTHORIZED
# --------------------------------------------------------------------------- #
def test_live_mode_with_default_read_only_state_is_rejected_before_risk_manager():
    account = TradingAccount(account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.LIVE)
    # A RiskLimits that would otherwise be LIVE-ready -- proves the
    # rejection comes from the NEW gate, not from Blocker E's own check.
    limits = RiskLimits(
        max_order_quantity=100, max_order_value=100000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=100000.0, max_account_exposure=100000.0,
    )
    engine = _engine_for(account, risk_limits=limits)
    result = engine.execute(_intent())
    assert result.success is False
    assert "LIVE" in result.message and "authorization_state" in result.message


def test_live_mode_with_canary_ready_state_is_still_rejected():
    """CANARY_READY is not sufficient for plain LIVE -- only LIVE_AUTHORIZED is."""
    account = TradingAccount(
        account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.LIVE,
        authorization_state=AccountAuthorizationState.CANARY_READY,
    )
    limits = RiskLimits(
        max_order_quantity=100, max_order_value=100000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=100000.0, max_account_exposure=100000.0,
    )
    engine = _engine_for(account, risk_limits=limits)
    result = engine.execute(_intent())
    assert result.success is False


def test_live_mode_with_live_authorized_state_passes_the_gate():
    account = TradingAccount(
        account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.LIVE,
        authorization_state=AccountAuthorizationState.LIVE_AUTHORIZED,
    )
    limits = RiskLimits(
        max_order_quantity=100, max_order_value=100000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=100000.0, max_account_exposure=100000.0,
    )
    engine = _engine_for(account, risk_limits=limits)
    result = engine.execute(_intent())
    assert result.success is True


def test_live_authorized_but_disabled_account_is_still_rejected():
    account = TradingAccount(
        account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.LIVE,
        authorization_state=AccountAuthorizationState.LIVE_AUTHORIZED,
    )
    engine = _engine_for(account)
    # Disabled AFTER assignment -- StrategyAssignment.assign() itself refuses
    # to assign an already-disabled account (matching the existing pattern
    # in tests/common/test_risk_manager.py::test_disabled_account_is_denied).
    account.enabled = False
    result = engine.execute(_intent())
    assert result.success is False


# --------------------------------------------------------------------------- #
# LIVE_CANARY: requires CANARY_READY or LIVE_AUTHORIZED
# --------------------------------------------------------------------------- #
def test_live_canary_with_default_read_only_state_is_rejected_before_canary_guard():
    account = TradingAccount(account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.LIVE_CANARY)
    guard = LiveCanaryGuard(CanaryLimits(
        account_id="ACC1", max_order_quantity=100, max_order_value=100000.0,
        max_daily_loss=5000.0, max_strategy_loss=5000.0, max_orders_per_day=10,
    ))
    engine = _engine_for(account, canary_guard=guard)
    result = engine.execute(_intent())
    assert result.success is False
    assert "LIVE_CANARY" in result.message and "authorization_state" in result.message


def test_live_canary_with_canary_ready_state_passes_the_gate():
    account = TradingAccount(
        account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.LIVE_CANARY,
        authorization_state=AccountAuthorizationState.CANARY_READY,
    )
    guard = LiveCanaryGuard(CanaryLimits(
        account_id="ACC1", max_order_quantity=100, max_order_value=100000.0,
        max_daily_loss=5000.0, max_strategy_loss=5000.0, max_orders_per_day=10,
    ))
    engine = _engine_for(account, canary_guard=guard)
    result = engine.execute(_intent())
    assert result.success is True


def test_gate_runs_before_risk_manager_not_instead_of_it():
    """A LIVE_AUTHORIZED account with a RiskLimits that breaches
    max_order_quantity must still be rejected -- the new gate does not
    bypass RiskManager, it only runs before it."""
    account = TradingAccount(
        account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=ExecutionMode.LIVE,
        authorization_state=AccountAuthorizationState.LIVE_AUTHORIZED,
    )
    limits = RiskLimits(
        max_order_quantity=1, max_order_value=100000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=100000.0, max_account_exposure=100000.0,
    )
    engine = _engine_for(account, risk_limits=limits)
    result = engine.execute(_intent(quantity=50))
    assert result.success is False
    assert "MAX_ORDER_QUANTITY" in result.message
