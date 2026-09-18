"""Phase 15D-DR: deployment, restart, crash-recovery, idempotency,
rollback, kill-switch, and trading-state recovery safety.

No real broker credential, network call, or mutation is used anywhere in
this file -- every scenario uses PaperBroker, in-process fakes, or a real
SqliteIdempotencyStore/CentralKillSwitch against a temp file (both are
pure local persistence, never a broker).
"""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest

from trading.common.broker import OrderResult, OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.execution import (
    AmbiguousOrderStateError,
    ExecutionConfig,
    StrategyExecutionEngine,
)
from trading.common.idempotency_store import (
    STATUS_AMBIGUOUS,
    STATUS_COMPLETED,
    STATUS_PENDING,
    AmbiguousIdempotencyStateError,
    IdempotencyRecord,
    SqliteIdempotencyStore,
    compute_intent_hash,
)
from trading.common.kill_switch import CentralKillSwitch
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskLimits, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import AccountAuthorizationState, ExecutionMode, TradingAccount

ACCOUNT_A = "ANGEL_SAMIR"


def _intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id="StrategyA", account_id=ACCOUNT_A, symbol="NIFTY24950CE", exchange="NFO",
        side=OrderSide.SELL, quantity=5, order_type=OrderType.LIMIT, limit_price=100.0,
        idempotency_key="idem-1",
    )
    fields.update(overrides)
    return OrderIntent(**fields)


class _RaisingBroker(PaperBroker):
    """A broker double whose place_order() raises a specific exception on
    the Nth call (simulating a network timeout/connection reset AFTER the
    call started) -- used to prove AmbiguousOrderStateError is raised
    instead of a blind retry, and that the retry loop calls place_order
    at most once per execute() invocation for this failure mode."""

    def __init__(self, fail_times: int = 1, exc: Exception | None = None) -> None:
        super().__init__()
        self.fail_times = fail_times
        self.exc = exc or ConnectionError("simulated network timeout")
        self.call_count = 0

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, limit_price=None):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise self.exc
        return super().place_order(symbol, side, quantity, order_type, limit_price)


def _stack(*, idempotency_store=None, central_kill_switch=None, broker=None, account_id=ACCOUNT_A):
    manager = BrokerManager()
    broker = broker or PaperBroker()
    manager.register_account(
        TradingAccount(account_id=account_id, account_name=account_id, broker_id="angelone", execution_mode=ExecutionMode.PAPER),
        broker_client=broker,
    )
    assignment = StrategyAssignment(manager)
    assignment.assign("StrategyA", account_id)
    risk_manager = RiskManager(assignment, limits=RiskLimits(
        max_order_quantity=100, max_order_value=100000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=100000.0, max_account_exposure=100000.0,
    ))
    engine = StrategyExecutionEngine(
        broker, ExecutionConfig(min_api_interval_seconds=0.0, retry_delay_seconds=0.0),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=manager,
        idempotency_store=idempotency_store, central_kill_switch=central_kill_switch,
    )
    return engine, broker, manager


# --------------------------------------------------------------------------- #
# AREA E/J: ambiguous broker response must never trigger a blind retry
# --------------------------------------------------------------------------- #
def test_ambiguous_broker_exception_does_not_retry_place_order():
    broker = _RaisingBroker(fail_times=99)  # would fail every attempt if retried
    engine, _, _ = _stack(broker=broker)
    result = engine.execute(_intent())
    assert result.success is False
    assert "ambiguous" in result.message.lower()
    assert broker.call_count == 1  # NEVER retried -- the whole point of this fix


def test_ambiguous_broker_exception_persists_status_ambiguous():
    store = SqliteIdempotencyStore(db_path=tempfile.mktemp(suffix=".db"))
    broker = _RaisingBroker(fail_times=99)
    engine, _, _ = _stack(broker=broker, idempotency_store=store)
    engine.execute(_intent(idempotency_key="ambiguous-key"))
    record = store.get("ambiguous-key")
    assert record is not None
    assert record.status == STATUS_AMBIGUOUS


def test_confirmed_rejected_status_is_still_safely_retried_with_new_price():
    """A definitive REJECTED response (not an exception) is a DIFFERENT,
    safe-to-retry case -- must not regress into the ambiguous path."""
    class _RejectOnceBroker(PaperBroker):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, limit_price=None):
            self.calls += 1
            if self.calls == 1:
                return OrderResult(order_id="", symbol=symbol, side=side, quantity=quantity, status="REJECTED", message="try again")
            return super().place_order(symbol, side, quantity, order_type, limit_price)

    broker = _RejectOnceBroker()
    engine, _, _ = _stack(broker=broker)
    result = engine.execute(_intent(idempotency_key="reject-once"))
    assert result.success is True
    assert broker.calls == 2  # confirmed rejection WAS safely retried


# --------------------------------------------------------------------------- #
# AREA C.8 / M.7: concurrent duplicate requests
# --------------------------------------------------------------------------- #
def test_concurrent_duplicate_requests_reach_the_broker_at_most_once():
    class _CountingBroker(PaperBroker):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()
            self.calls = 0

        def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, limit_price=None):
            with self.lock:
                self.calls += 1
            return super().place_order(symbol, side, quantity, order_type, limit_price)

    store = SqliteIdempotencyStore(db_path=tempfile.mktemp(suffix=".db"))
    broker = _CountingBroker()
    engine, _, _ = _stack(broker=broker, idempotency_store=store)

    results = []

    def _run():
        try:
            results.append(engine.execute(_intent(idempotency_key="race-key")))
        except AmbiguousIdempotencyStateError:
            # Expected for a thread that loses the claim() race AFTER the
            # winning thread's PENDING record is already visible -- this
            # propagates loudly by design (same convention as the
            # pre-existing IdempotencyKeyReuseError), never silently
            # swallowed. Not appended to `results` -- this thread never
            # got a definitive outcome to report.
            pass

    threads = [threading.Thread(target=_run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert broker.calls == 1  # the atomic claim() must prevent every duplicate
    successes = [r for r in results if r.success]
    assert len(successes) == 1


def test_duplicate_idempotency_request_replays_without_a_second_broker_call():
    store = SqliteIdempotencyStore(db_path=tempfile.mktemp(suffix=".db"))
    engine, broker, _ = _stack(idempotency_store=store)
    first = engine.execute(_intent(idempotency_key="dup-key"))
    second = engine.execute(_intent(idempotency_key="dup-key"))
    assert first.success is True
    assert second.order_id == first.order_id


# --------------------------------------------------------------------------- #
# AREA B/C: restart scenarios (simulated via a fresh store/engine pointed at
# the same underlying SQLite file)
# --------------------------------------------------------------------------- #
def test_restart_before_intent_has_no_stale_record():
    db_path = tempfile.mktemp(suffix=".db")
    store = SqliteIdempotencyStore(db_path=db_path)
    assert store.get("never-used-key") is None
    # "restart": a brand-new store instance against the same file
    store2 = SqliteIdempotencyStore(db_path=db_path)
    assert store2.get("never-used-key") is None


def test_restart_after_idempotency_persistence_replays_correctly():
    db_path = tempfile.mktemp(suffix=".db")
    store = SqliteIdempotencyStore(db_path=db_path)
    engine, broker, _ = _stack(idempotency_store=store)
    first = engine.execute(_intent(idempotency_key="restart-key"))
    assert first.success is True

    # "restart": fresh store + fresh engine, same underlying file/broker class
    store_after_restart = SqliteIdempotencyStore(db_path=db_path)
    broker2 = PaperBroker()
    engine2, _, _ = _stack(idempotency_store=store_after_restart, broker=broker2)
    second = engine2.execute(_intent(idempotency_key="restart-key"))
    assert second.order_id == first.order_id  # replayed, not resubmitted


def test_restart_after_ambiguous_broker_response_requires_reconciliation_not_retry():
    db_path = tempfile.mktemp(suffix=".db")
    store = SqliteIdempotencyStore(db_path=db_path)
    broker = _RaisingBroker(fail_times=99)
    engine, _, _ = _stack(broker=broker, idempotency_store=store)
    engine.execute(_intent(idempotency_key="ambiguous-restart-key"))

    # "restart"
    store_after_restart = SqliteIdempotencyStore(db_path=db_path)
    engine2, broker2, _ = _stack(idempotency_store=store_after_restart)
    with pytest.raises(AmbiguousIdempotencyStateError):
        engine2.execute(_intent(idempotency_key="ambiguous-restart-key"))
    assert broker2.get_positions() == []  # no order was ever placed on the fresh broker either


def test_restart_with_a_leftover_pending_record_is_never_silently_resumed():
    """Simulates a crash: a claim() succeeded (PENDING was written) but the
    process died before ever reaching a definitive outcome. After
    'restart', the same key must NOT be silently replayed or resubmitted."""
    db_path = tempfile.mktemp(suffix=".db")
    store = SqliteIdempotencyStore(db_path=db_path)
    intent = _intent(idempotency_key="crashed-key")
    store.claim(intent.idempotency_key, strategy_id=intent.strategy_id, account_id=intent.account_id, intent_hash=compute_intent_hash(intent))

    store_after_restart = SqliteIdempotencyStore(db_path=db_path)
    engine2, broker2, _ = _stack(idempotency_store=store_after_restart)
    with pytest.raises(AmbiguousIdempotencyStateError):
        engine2.execute(intent)
    assert broker2.get_positions() == []


def test_observability_failure_after_broker_acceptance_does_not_retry_or_duplicate():
    """Phase 14.6 Blocker C, re-verified under this phase's own lens: a
    broker success followed by an audit/metrics failure must not cause a
    second broker call, and the broker result remains authoritative."""
    from trading.common.observability import AuditTrail

    class _BrokenAuditTrail(AuditTrail):
        def append(self, *a, **k):
            raise RuntimeError("simulated observability failure")

    store = SqliteIdempotencyStore(db_path=tempfile.mktemp(suffix=".db"))
    manager = BrokerManager()
    broker = PaperBroker()
    manager.register_account(
        TradingAccount(account_id=ACCOUNT_A, account_name=ACCOUNT_A, broker_id="angelone", execution_mode=ExecutionMode.PAPER),
        broker_client=broker,
    )
    assignment = StrategyAssignment(manager)
    assignment.assign("StrategyA", ACCOUNT_A)
    risk_manager = RiskManager(assignment, limits=RiskLimits(
        max_order_quantity=100, max_order_value=100000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=100000.0, max_account_exposure=100000.0,
    ))
    engine = StrategyExecutionEngine(
        broker, ExecutionConfig(min_api_interval_seconds=0.0),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=manager,
        idempotency_store=store, audit_trail=_BrokenAuditTrail(),
    )
    result = engine.execute(_intent(idempotency_key="obs-fail-key"))
    assert result.success is True  # broker result remains authoritative
    record = store.get("obs-fail-key")
    assert record is not None and record.status == STATUS_COMPLETED


# --------------------------------------------------------------------------- #
# AREA F: kill switch persistence across restart
# --------------------------------------------------------------------------- #
def test_kill_switch_without_persistence_path_keeps_legacy_in_memory_behavior():
    ks = CentralKillSwitch()
    ks.engage(by="test", reason="drill")
    assert ks.engaged is True
    ks2 = CentralKillSwitch()  # a fresh instance, no persistence -- must NOT see the other's state
    assert ks2.engaged is False


def test_kill_switch_persists_engaged_state_across_restart():
    path = Path(tempfile.mktemp(suffix=".json"))
    ks = CentralKillSwitch(persistence_path=path)
    ks.engage(by="ops", reason="real incident")
    assert ks.engaged is True

    # "restart": a fresh instance reading the same persistence file
    ks_after_restart = CentralKillSwitch(persistence_path=path)
    assert ks_after_restart.engaged is True
    assert ks_after_restart.reason == "real incident"


def test_kill_switch_persists_disengaged_state_across_restart_too():
    path = Path(tempfile.mktemp(suffix=".json"))
    ks = CentralKillSwitch(persistence_path=path)
    ks.engage(by="ops", reason="drill")
    ks.disengage(by="ops")
    ks_after_restart = CentralKillSwitch(persistence_path=path)
    assert ks_after_restart.engaged is False


def test_kill_switch_blocks_execution_and_persists_through_restart_end_to_end():
    path = Path(tempfile.mktemp(suffix=".json"))
    ks = CentralKillSwitch(persistence_path=path)
    ks.engage(by="ops", reason="halt all trading")
    engine, _, _ = _stack(central_kill_switch=ks)
    result = engine.execute(_intent())
    assert result.success is False
    assert "kill switch" in result.message.lower()

    ks_after_restart = CentralKillSwitch(persistence_path=path)
    engine2, _, _ = _stack(central_kill_switch=ks_after_restart)
    result2 = engine2.execute(_intent(idempotency_key="idem-2"))
    assert result2.success is False  # remains ON after restart -- not silently reset


def test_kill_switch_unreadable_persistence_file_fails_closed_engaged():
    path = Path(tempfile.mktemp(suffix=".json"))
    path.write_text("{not valid json", encoding="utf-8")
    ks = CentralKillSwitch(persistence_path=path)
    assert ks.engaged is True  # fail closed, never silently disengaged on a read error


# --------------------------------------------------------------------------- #
# AREA G: account isolation persists correctly across a simulated restart
# --------------------------------------------------------------------------- #
def test_account_isolation_holds_across_restart_simulated_idempotency_store():
    db_path = tempfile.mktemp(suffix=".db")
    store = SqliteIdempotencyStore(db_path=db_path)
    engine_a, broker_a, _ = _stack(idempotency_store=store, account_id="ANGEL_SAMIR")
    engine_b, broker_b, _ = _stack(idempotency_store=store, account_id="ANGEL_ACCOUNT_B")

    engine_a.execute(_intent(account_id="ANGEL_SAMIR", idempotency_key="acct-key-a"))
    engine_b.execute(_intent(account_id="ANGEL_ACCOUNT_B", strategy_id="StrategyA", idempotency_key="acct-key-b"))

    # "restart"
    store_after_restart = SqliteIdempotencyStore(db_path=db_path)
    record_a = store_after_restart.get("acct-key-a")
    record_b = store_after_restart.get("acct-key-b")
    assert record_a.account_id == "ANGEL_SAMIR"
    assert record_b.account_id == "ANGEL_ACCOUNT_B"
    assert record_a.account_id != record_b.account_id


# --------------------------------------------------------------------------- #
# AREA I: persistence failure fails closed (idempotency store unavailable)
# --------------------------------------------------------------------------- #
def test_idempotency_store_unavailable_fails_closed_not_silently_in_memory():
    """A store whose get()/claim() raise (simulating 'database unavailable')
    must cause execute() to fail closed -- never silently fall back to
    treating the order as if no idempotency protection existed."""

    class _BrokenStore:
        def get(self, key):
            raise RuntimeError("database unavailable")

        def put(self, record):
            raise RuntimeError("database unavailable")

        def claim(self, key, *, strategy_id, account_id, intent_hash):
            raise RuntimeError("database unavailable")

    engine, _, _ = _stack(idempotency_store=_BrokenStore())
    with pytest.raises(RuntimeError):
        engine.execute(_intent(idempotency_key="broken-store-key"))


# --------------------------------------------------------------------------- #
# AREA B: authorization state after restart -- READ_ONLY is the class default,
# so a freshly (re)constructed TradingAccount is always safe by construction
# --------------------------------------------------------------------------- #
def test_freshly_constructed_account_after_restart_defaults_to_read_only():
    """There is no persistent TradingAccount store in this codebase --
    every account is (re)constructed from code/config at process start.
    This test documents and proves the resulting safety property: a
    'restart' can never accidentally resume a promoted authorization_state,
    because no promoted state is ever persisted in the first place."""
    account = TradingAccount(account_id=ACCOUNT_A, account_name=ACCOUNT_A, broker_id="angelone")
    assert account.authorization_state == AccountAuthorizationState.READ_ONLY
    assert account.is_live_authorized() is False


def test_killed_state_has_no_reversal_path_even_conceptually():
    account = TradingAccount(account_id=ACCOUNT_A, account_name=ACCOUNT_A, broker_id="angelone")
    account.set_killed(reason="restart-safety-test")
    assert account.authorization_state == AccountAuthorizationState.KILLED
    assert not hasattr(account, "un_kill") and not hasattr(account, "revive") and not hasattr(account, "resume")


# --------------------------------------------------------------------------- #
# AREA L: deployment/health must never imply trading authorization
# --------------------------------------------------------------------------- #
def test_deployment_info_carries_no_trading_authorization_signal():
    from trading.common.deployment_info import get_deployment_info

    info = get_deployment_info()
    assert not hasattr(info, "trading_authorized")
    assert not hasattr(info, "live_authorized")


def test_readiness_endpoint_never_reports_trading_authorized_true():
    from trading.api.health import ready

    result = ready()
    assert result.trading_authorized is False
