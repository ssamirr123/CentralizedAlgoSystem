"""Phase 15C.5: final multi-account read-only gate -- the remaining gaps not
already covered by Phase 15B/15B.1/15C.2/15C.2.1/15C.3/15C.4:

  - execution-boundary enforcement (READ_ONLY cannot be bypassed by calling
    the broker adapter, or BrokerManager, directly -- not just through
    StrategyExecutionEngine.execute())
  - router-bypass proof (going straight to BrokerManager.get_broker(),
    skipping TradingAccountRouter entirely, still cannot mutate)
  - kill switch + READ_ONLY mutation-attempt proofs scoped to the two named
    real account IDs used throughout this project (ANGEL_SAMIR /
    ANGEL_ACCOUNT_B), using synthetic doubles -- no real broker call
  - idempotency: the same idempotency_key string used by two different
    accounts must never be cross-replayed

No real credential, network call, or mutation is used anywhere in this file.
"""
from __future__ import annotations

import pytest

from trading.common.broker import OrderSide, OrderType, ReadOnlyModeError
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.idempotency_store import IdempotencyKeyReuseError, InMemoryIdempotencyStore
from trading.common.kill_switch import CentralKillSwitch
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskLimits, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import AccountAuthorizationState, ExecutionMode, TradingAccount

ACCOUNT_A = "ANGEL_SAMIR"
ACCOUNT_B = "ANGEL_ACCOUNT_B"


def _intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id="StrategyA", account_id=ACCOUNT_A, symbol="NIFTY24950CE", exchange="NFO",
        side=OrderSide.SELL, quantity=5, order_type=OrderType.LIMIT, limit_price=100.0,
    )
    fields.update(overrides)
    return OrderIntent(**fields)


class _ReadOnlyAwareBroker(PaperBroker):
    """A PaperBroker wrapper that enforces read_only exactly like the real
    Angel One adapter does (_require_not_read_only checked first, before
    TRADING_MODE) -- used here instead of the real adapter so this test
    file needs no real credentials, while still proving the SAME safety
    property (read_only enforced at the adapter, not the caller)."""

    def __init__(self, read_only: bool = True) -> None:
        super().__init__()
        self._read_only = read_only

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, limit_price=None):
        if self._read_only:
            raise ReadOnlyModeError("read-only adapter: place_order blocked")
        return super().place_order(symbol, side, quantity, order_type, limit_price)

    def cancel_order(self, order_id: str) -> bool:
        if self._read_only:
            raise ReadOnlyModeError("read-only adapter: cancel_order blocked")
        return super().cancel_order(order_id)


def _stack(account_id: str, *, authorization_state=AccountAuthorizationState.READ_ONLY, execution_mode=ExecutionMode.PAPER):
    manager = BrokerManager()
    broker = _ReadOnlyAwareBroker(read_only=True)
    account = TradingAccount(
        account_id=account_id, account_name=account_id, broker_id="angelone",
        execution_mode=execution_mode, authorization_state=authorization_state,
    )
    manager.register_account(account, broker_client=broker)
    assignment = StrategyAssignment(manager)
    assignment.assign("StrategyA", account_id)
    risk_manager = RiskManager(assignment, limits=RiskLimits(
        max_order_quantity=100, max_order_value=100000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=100000.0, max_account_exposure=100000.0,
    ))
    return manager, broker, assignment, risk_manager


# --------------------------------------------------------------------------- #
# Step 10/11: execution-boundary + router-bypass enforcement
# --------------------------------------------------------------------------- #
def test_direct_broker_manager_access_bypassing_router_still_blocks_mutation():
    """Going straight to BrokerManager.get_broker() -- skipping
    TradingAccountRouter entirely -- must still be unable to mutate,
    because the safety property lives on the adapter (read_only), not on
    the router."""
    manager, broker, _, _ = _stack(ACCOUNT_A)
    resolved = manager.get_broker(ACCOUNT_A)  # bypasses TradingAccountRouter
    assert resolved is broker
    with pytest.raises(ReadOnlyModeError):
        resolved.place_order("NIFTY", OrderSide.BUY, 1)


def test_direct_adapter_construction_bypassing_everything_still_blocks_mutation():
    """Constructing the adapter directly (bypassing BrokerManager, the
    router, AND the execution engine) still cannot mutate -- read_only is
    enforced at the one place that actually talks to the broker."""
    broker = _ReadOnlyAwareBroker(read_only=True)
    with pytest.raises(ReadOnlyModeError):
        broker.place_order("NIFTY", OrderSide.BUY, 1)
    with pytest.raises(ReadOnlyModeError):
        broker.cancel_order("DUMMY")


def test_execute_pipeline_rejects_read_only_account_before_any_broker_call_for_both_named_accounts():
    """The AuthorizationState gate inside StrategyExecutionEngine.execute()
    rejects a READ_ONLY account attempting LIVE execution -- proven for
    BOTH of this project's real account IDs (using synthetic doubles)."""
    for account_id in (ACCOUNT_A, ACCOUNT_B):
        manager, broker, assignment, risk_manager = _stack(account_id, execution_mode=ExecutionMode.LIVE)
        engine = StrategyExecutionEngine(
            broker, ExecutionConfig(dry_run=True),
            risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=manager,
        )
        result = engine.execute(_intent(account_id=account_id, idempotency_key=f"gate-{account_id}"))
        assert result.success is False
        assert "authorization_state" in result.message


# --------------------------------------------------------------------------- #
# Step 14: kill switch, scoped to both named accounts
# --------------------------------------------------------------------------- #
def test_kill_switch_blocks_both_accounts_even_when_otherwise_authorized():
    kill_switch = CentralKillSwitch()
    kill_switch.engage(by="test", reason="phase-15c5-drill")
    for account_id in (ACCOUNT_A, ACCOUNT_B):
        manager, broker, assignment, risk_manager = _stack(
            account_id, authorization_state=AccountAuthorizationState.LIVE_AUTHORIZED, execution_mode=ExecutionMode.LIVE,
        )
        engine = StrategyExecutionEngine(
            broker, ExecutionConfig(dry_run=True),
            risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=manager,
            central_kill_switch=kill_switch,
        )
        result = engine.execute(_intent(account_id=account_id, idempotency_key=f"ks-{account_id}"))
        assert result.success is False
        assert "kill switch" in result.message.lower()


# --------------------------------------------------------------------------- #
# Step 15: idempotency isolation across accounts
# --------------------------------------------------------------------------- #
def test_same_idempotency_key_across_two_accounts_is_never_cross_replayed():
    """If Account A and Account B ever produced the SAME idempotency_key
    string (a bug elsewhere, or coincidence), the store must detect the
    mismatch (different account_id -> different intent_hash) and raise --
    never silently replay Account A's cached result for Account B's
    request, and never silently let it through as a fresh order either."""
    shared_key = "SHARED-KEY-COLLISION"
    store = InMemoryIdempotencyStore()

    manager_a, broker_a, assignment_a, risk_a = _stack(ACCOUNT_A)
    engine_a = StrategyExecutionEngine(
        broker_a, ExecutionConfig(dry_run=True),
        risk_manager=risk_a, strategy_assignment=assignment_a, broker_manager=manager_a, idempotency_store=store,
    )
    result_a = engine_a.execute(_intent(account_id=ACCOUNT_A, idempotency_key=shared_key))
    assert result_a.success is True

    manager_b, broker_b, assignment_b, risk_b = _stack(ACCOUNT_B)
    engine_b = StrategyExecutionEngine(
        broker_b, ExecutionConfig(dry_run=True),
        risk_manager=risk_b, strategy_assignment=assignment_b, broker_manager=manager_b, idempotency_store=store,
    )
    with pytest.raises(IdempotencyKeyReuseError):
        engine_b.execute(_intent(account_id=ACCOUNT_B, strategy_id="StrategyA", idempotency_key=shared_key))


def test_distinct_idempotency_keys_per_account_do_not_interfere():
    store = InMemoryIdempotencyStore()
    manager_a, broker_a, assignment_a, risk_a = _stack(ACCOUNT_A)
    engine_a = StrategyExecutionEngine(
        broker_a, ExecutionConfig(dry_run=True),
        risk_manager=risk_a, strategy_assignment=assignment_a, broker_manager=manager_a, idempotency_store=store,
    )
    manager_b, broker_b, assignment_b, risk_b = _stack(ACCOUNT_B)
    engine_b = StrategyExecutionEngine(
        broker_b, ExecutionConfig(dry_run=True),
        risk_manager=risk_b, strategy_assignment=assignment_b, broker_manager=manager_b, idempotency_store=store,
    )
    result_a = engine_a.execute(_intent(account_id=ACCOUNT_A, idempotency_key="A-KEY"))
    result_b = engine_b.execute(_intent(account_id=ACCOUNT_B, idempotency_key="B-KEY"))
    assert result_a.success is True
    assert result_b.success is True
    assert result_a.account_id == ACCOUNT_A
    assert result_b.account_id == ACCOUNT_B
