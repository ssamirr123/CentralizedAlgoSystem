"""Phase 17.2-P: PortfolioRiskManager wired to a real durable
SqlitePortfolioRiskStore -- restart recovery, the crash matrix, cross-store
consistency with IdempotencyStore, readiness gating, and trading-day
rollover. No real broker, no real network -- FakeBroker-equivalent
ExecutionResult objects only, matching every other Phase 17.x safety test
file in this repo.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from trading.common.broker import OrderSide, OrderType
from trading.common.execution import ExecutionResult
from trading.common.idempotency_store import (
    STATUS_AMBIGUOUS,
    STATUS_COMPLETED,
    STATUS_REJECTED,
    IdempotencyRecord,
    InMemoryIdempotencyStore,
)
from trading.common.order_intent import OrderIntent
from trading.common.portfolio_risk import PortfolioRiskLimits, PortfolioRiskManager
from trading.common.portfolio_risk_store import PortfolioRiskReadiness, open_or_diagnose

ACCOUNT_A = "ACC_A"
ACCOUNT_B = "ACC_B"


def _intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id="StrategyA", account_id=ACCOUNT_A, symbol="NIFTY", exchange="NFO",
        side=OrderSide.BUY, quantity=10, order_type=OrderType.LIMIT, limit_price=100.0,
        idempotency_key="idem-1",
    )
    fields.update(overrides)
    return OrderIntent(**fields)


def _db(tmp_path: Path, name: str = "risk.db") -> str:
    return str(tmp_path / name)


def _manager(tmp_path, *, name="risk.db", idempotency_store=None, limits=None) -> PortfolioRiskManager:
    store, readiness, detail = open_or_diagnose(_db(tmp_path, name))
    assert readiness == PortfolioRiskReadiness.READY, detail
    return PortfolioRiskManager(portfolio_limits=limits, store=store, readiness=readiness, idempotency_store=idempotency_store)


# --------------------------------------------------------------------------- #
# Basic durability: reserve/commit/release survive a fresh instance (restart)
# --------------------------------------------------------------------------- #
def test_outstanding_reservation_survives_restart(tmp_path):
    """Section 32: reserve, persist, CRASH (simulated by discarding the
    Python object and constructing a fresh one against the same db)."""
    mgr1 = _manager(tmp_path)
    decision = mgr1.evaluate_and_reserve(_intent(), strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert decision.allowed is True
    reservation_id = decision.reservation_id
    del mgr1  # simulate process crash -- no clean shutdown, no explicit flush

    mgr2 = _manager(tmp_path)
    snapshot = mgr2.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 1000.0  # still counted -- not silently zeroed
    assert snapshot.strategy_open_orders == 1
    assert reservation_id in mgr2._reservations  # recovered into the SAME live dict every other method reads


def test_committed_exposure_survives_restart(tmp_path):
    mgr1 = _manager(tmp_path)
    decision = mgr1.evaluate_and_reserve(_intent(idempotency_key="idem-committed"), strategy_id="StrategyA", account_id=ACCOUNT_A)
    mgr1.commit_reservation(decision.reservation_id, execution_result=ExecutionResult(success=True, status="COMPLETE"))
    del mgr1

    mgr2 = _manager(tmp_path)
    snapshot = mgr2.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 1000.0  # ledger position, not a reservation, still counted


def test_released_reservation_does_not_reappear_after_restart(tmp_path):
    mgr1 = _manager(tmp_path)
    decision = mgr1.evaluate_and_reserve(_intent(idempotency_key="idem-released"), strategy_id="StrategyA", account_id=ACCOUNT_A)
    mgr1.release_reservation(decision.reservation_id)
    del mgr1

    mgr2 = _manager(tmp_path)
    snapshot = mgr2.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 0.0
    assert snapshot.strategy_open_orders == 0


def test_ambiguous_reservation_survives_restart_held(tmp_path):
    """Section 33: reserve, execution attempt, AMBIGUOUS, CRASH, restart."""
    mgr1 = _manager(tmp_path)
    intent = _intent(idempotency_key="idem-ambig")
    decision = mgr1.evaluate_and_reserve(intent, strategy_id="StrategyA", account_id=ACCOUNT_A)
    mgr1.commit_reservation(decision.reservation_id, execution_result=ExecutionResult.ambiguous(intent, "network timeout"))
    del mgr1

    mgr2 = _manager(tmp_path)
    snapshot = mgr2.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 1000.0  # held -- unknown never became zero
    assert decision.reservation_id in mgr2._open_orders
    # No automatic retry exists anywhere in this class -- there is no
    # method that would ever re-attempt execution; the only way forward is
    # resolve_reconciliation(), exercised below.


# --------------------------------------------------------------------------- #
# Readiness / fail-closed gating (Section 21-24, 57-59)
# --------------------------------------------------------------------------- #
def test_missing_expected_db_blocks_new_reservations(tmp_path):
    open_or_diagnose(_db(tmp_path))  # first run
    Path(_db(tmp_path)).unlink()  # unexpected loss
    store, readiness, detail = open_or_diagnose(_db(tmp_path))
    assert readiness == PortfolioRiskReadiness.NOT_READY
    mgr = PortfolioRiskManager(store=store, readiness=readiness)
    assert mgr.readiness == PortfolioRiskReadiness.NOT_READY

    decision = mgr.evaluate_and_reserve(_intent(), strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert decision.allowed is False
    assert decision.reason_code == "RISK_NOT_READY"


def test_corrupt_db_blocks_new_reservations(tmp_path):
    open_or_diagnose(_db(tmp_path))
    with open(_db(tmp_path), "wb") as f:
        f.write(b"corrupted, not a real database")
    store, readiness, detail = open_or_diagnose(_db(tmp_path))
    mgr = PortfolioRiskManager(store=store, readiness=readiness)
    decision = mgr.evaluate_and_reserve(_intent(), strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert decision.allowed is False
    assert decision.reason_code == "RISK_NOT_READY"


def test_default_in_memory_manager_is_always_ready():
    """Zero behavior change for every pre-existing caller that never wires
    a store at all."""
    mgr = PortfolioRiskManager()
    assert mgr.readiness == PortfolioRiskReadiness.READY
    decision = mgr.evaluate_and_reserve(_intent(), strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert decision.allowed is True


# --------------------------------------------------------------------------- #
# Cross-store consistency with IdempotencyStore (Section 29-30, crash cells F/G/K/L)
# --------------------------------------------------------------------------- #
def _idem_record(key, status, **kw) -> IdempotencyRecord:
    fields = dict(
        idempotency_key=key, strategy_id="StrategyA", account_id=ACCOUNT_A, intent_hash="h",
        status=status, broker_order_id="", result_json="", created_at="now", updated_at="now",
    )
    fields.update(kw)
    return IdempotencyRecord(**fields)


def test_recovery_converges_ambiguous_to_committed_when_idempotency_says_completed(tmp_path):
    """Crash cell F: success confirmed at the broker/idempotency layer,
    but the process died before PortfolioRisk's own commit was durable.
    On restart, cross-referencing IdempotencyStore must converge this to
    COMMITTED, never leave it stuck AMBIGUOUS forever."""
    mgr1 = _manager(tmp_path)
    intent = _intent(idempotency_key="idem-f")
    decision = mgr1.evaluate_and_reserve(intent, strategy_id="StrategyA", account_id=ACCOUNT_A)
    mgr1.commit_reservation(decision.reservation_id, execution_result=ExecutionResult.ambiguous(intent, "unknown"))
    del mgr1

    idem_store = InMemoryIdempotencyStore()
    idem_store.put(_idem_record("idem-f", STATUS_COMPLETED))

    mgr2 = _manager(tmp_path, idempotency_store=idem_store)
    snapshot = mgr2.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_open_orders == 0  # no longer outstanding -- converged to a ledger fill
    assert snapshot.strategy_exposure == 1000.0  # exposure preserved as a real position, not lost


def test_recovery_converges_ambiguous_to_released_when_idempotency_says_rejected(tmp_path):
    """Crash cell G: confirmed rejection at the idempotency layer, but the
    process died before PortfolioRisk's own release was durable."""
    mgr1 = _manager(tmp_path)
    intent = _intent(idempotency_key="idem-g")
    decision = mgr1.evaluate_and_reserve(intent, strategy_id="StrategyA", account_id=ACCOUNT_A)
    mgr1.commit_reservation(decision.reservation_id, execution_result=ExecutionResult.ambiguous(intent, "unknown"))
    del mgr1

    idem_store = InMemoryIdempotencyStore()
    idem_store.put(_idem_record("idem-g", STATUS_REJECTED))

    mgr2 = _manager(tmp_path, idempotency_store=idem_store)
    snapshot = mgr2.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 0.0  # released -- confirmed it never happened
    assert snapshot.strategy_open_orders == 0


def test_recovery_leaves_ambiguous_held_when_idempotency_still_ambiguous(tmp_path):
    """Cell J: reconciliation genuinely unresolved -- must stay held, not
    guessed in either direction."""
    mgr1 = _manager(tmp_path)
    intent = _intent(idempotency_key="idem-j")
    decision = mgr1.evaluate_and_reserve(intent, strategy_id="StrategyA", account_id=ACCOUNT_A)
    mgr1.commit_reservation(decision.reservation_id, execution_result=ExecutionResult.ambiguous(intent, "unknown"))
    del mgr1

    idem_store = InMemoryIdempotencyStore()
    idem_store.put(_idem_record("idem-j", STATUS_AMBIGUOUS))

    mgr2 = _manager(tmp_path, idempotency_store=idem_store)
    snapshot = mgr2.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 1000.0  # still held
    assert snapshot.strategy_open_orders == 1


def test_recovery_leaves_reservation_untouched_when_no_idempotency_record_exists_yet(tmp_path):
    """Crash cell C/D: reservation persisted, crash happened BEFORE the
    idempotency claim was ever made (execute()'s own gate order: risk ->
    mode gate -> broker resolution -> LiveAuthorization -> idempotency
    claim -> broker call). No idempotency row exists at all yet -- this is
    genuinely still in-flight, not an error, and must remain RESERVED."""
    mgr1 = _manager(tmp_path)
    decision = mgr1.evaluate_and_reserve(_intent(idempotency_key="idem-c"), strategy_id="StrategyA", account_id=ACCOUNT_A)
    del mgr1  # crash before ANY execute() call even started

    idem_store = InMemoryIdempotencyStore()  # empty -- no record for idem-c
    mgr2 = _manager(tmp_path, idempotency_store=idem_store)
    snapshot = mgr2.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 1000.0
    assert decision.reservation_id in mgr2._reservations  # still plain RESERVED, not moved anywhere


# --------------------------------------------------------------------------- #
# Idempotent repeated resolution (Section 42-44)
# --------------------------------------------------------------------------- #
def test_commit_called_twice_does_not_double_add_exposure(tmp_path):
    mgr = _manager(tmp_path)
    decision = mgr.evaluate_and_reserve(_intent(idempotency_key="idem-double-commit"), strategy_id="StrategyA", account_id=ACCOUNT_A)
    result = ExecutionResult(success=True, status="COMPLETE")
    mgr.commit_reservation(decision.reservation_id, execution_result=result)
    mgr.commit_reservation(decision.reservation_id, execution_result=result)  # no-op -- already popped from _reservations
    snapshot = mgr.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 1000.0  # not 2000


def test_release_called_twice_does_not_double_subtract(tmp_path):
    mgr = _manager(tmp_path)
    decision = mgr.evaluate_and_reserve(_intent(idempotency_key="idem-double-release"), strategy_id="StrategyA", account_id=ACCOUNT_A)
    mgr.release_reservation(decision.reservation_id)
    mgr.release_reservation(decision.reservation_id)  # no-op
    snapshot = mgr.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 0.0
    assert snapshot.strategy_orders_today == 0  # not negative


def test_duplicate_idempotency_key_same_day_produces_one_durable_reservation(tmp_path):
    """Section 40: caller-level dedup (WorkerCoordinator normally skips
    reservation entirely on replay) -- this proves the STORE itself would
    also refuse a genuine duplicate reservation_id, the structural backstop."""
    store, readiness, _ = open_or_diagnose(_db(tmp_path))
    store.create_reservation(
        reservation_id="only-once", idempotency_key="dup-key", strategy_id="S", account_id="A", symbol="X",
        side="BUY", quantity=1, price=1.0, notional=1.0, order_count_day=date(2026, 9, 20),
    )
    with pytest.raises(Exception):
        store.create_reservation(
            reservation_id="only-once", idempotency_key="dup-key", strategy_id="S", account_id="A", symbol="X",
            side="BUY", quantity=1, price=1.0, notional=1.0, order_count_day=date(2026, 9, 20),
        )


# --------------------------------------------------------------------------- #
# Daily order counter persistence + trading-day rollover (Section 45-47)
# --------------------------------------------------------------------------- #
def test_daily_order_counter_persists_across_restart(tmp_path):
    today = date(2026, 9, 20)
    mgr1 = _manager(tmp_path)
    mgr1._clock = lambda: __import__("datetime").datetime(2026, 9, 20, 10, 0, tzinfo=__import__("datetime").timezone.utc)
    for i in range(3):
        mgr1.evaluate_and_reserve(_intent(idempotency_key=f"idem-day-{i}"), strategy_id="StrategyA", account_id=ACCOUNT_A)
    del mgr1

    mgr2 = _manager(tmp_path)
    mgr2._clock = lambda: __import__("datetime").datetime(2026, 9, 20, 11, 0, tzinfo=__import__("datetime").timezone.utc)
    snapshot = mgr2.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_orders_today == 3  # restart on the SAME trading day preserves the count


def test_trading_day_rollover_starts_a_fresh_counter(tmp_path):
    import datetime as dt

    mgr1 = _manager(tmp_path)
    mgr1._clock = lambda: dt.datetime(2026, 9, 20, 10, 0, tzinfo=dt.timezone.utc)
    mgr1.evaluate_and_reserve(_intent(idempotency_key="idem-dayA"), strategy_id="StrategyA", account_id=ACCOUNT_A)
    del mgr1

    mgr2 = _manager(tmp_path)
    mgr2._clock = lambda: dt.datetime(2026, 9, 21, 10, 0, tzinfo=dt.timezone.utc)  # next trading day
    snapshot = mgr2.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_orders_today == 0  # fresh counter for the new day

    # And the day-1 outstanding reservation's own exposure/open-order
    # footprint must NOT be expired merely because the date changed --
    # daily counter and reservation lifecycle are different concepts
    # (Section 47).
    assert snapshot.strategy_exposure == 1000.0
    assert snapshot.strategy_open_orders == 1


# --------------------------------------------------------------------------- #
# Multiple strategies / accounts / same symbol (Section 36-38)
# --------------------------------------------------------------------------- #
def test_multiple_strategies_same_account_restart_correctly(tmp_path):
    mgr1 = _manager(tmp_path)
    mgr1.evaluate_and_reserve(
        _intent(strategy_id="CombinedVwapNifty", idempotency_key="idem-cv", quantity=5, limit_price=100.0),
        strategy_id="CombinedVwapNifty", account_id=ACCOUNT_A,
    )
    mgr1.evaluate_and_reserve(
        _intent(strategy_id="DoubleStraddelAlgo", idempotency_key="idem-ds", quantity=3, limit_price=200.0),
        strategy_id="DoubleStraddelAlgo", account_id=ACCOUNT_A,
    )
    del mgr1

    mgr2 = _manager(tmp_path)
    cv_snapshot = mgr2.get_snapshot(strategy_id="CombinedVwapNifty", account_id=ACCOUNT_A)
    ds_snapshot = mgr2.get_snapshot(strategy_id="DoubleStraddelAlgo", account_id=ACCOUNT_A)
    assert cv_snapshot.strategy_exposure == 500.0
    assert ds_snapshot.strategy_exposure == 600.0
    assert cv_snapshot.account_exposure == 1100.0  # both strategies aggregate correctly on the shared account
    assert ds_snapshot.portfolio_exposure == 1100.0


def test_multiple_accounts_isolated_after_restart(tmp_path):
    mgr1 = _manager(tmp_path)
    mgr1.evaluate_and_reserve(
        _intent(account_id=ACCOUNT_A, idempotency_key="idem-a", quantity=5, limit_price=100.0),
        strategy_id="StrategyA", account_id=ACCOUNT_A,
    )
    mgr1.evaluate_and_reserve(
        _intent(account_id=ACCOUNT_B, idempotency_key="idem-b", quantity=5, limit_price=100.0),
        strategy_id="StrategyA", account_id=ACCOUNT_B,
    )
    del mgr1

    mgr2 = _manager(tmp_path)
    a_snapshot = mgr2.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    b_snapshot = mgr2.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_B)
    assert a_snapshot.account_exposure == 500.0
    assert b_snapshot.account_exposure == 500.0
    assert a_snapshot.portfolio_exposure == 1000.0  # no leakage, correct aggregate


def test_same_symbol_two_strategies_additive_not_overwritten(tmp_path):
    mgr1 = _manager(tmp_path)
    intent1 = _intent(strategy_id="CombinedVwapNifty", symbol="NIFTY", idempotency_key="idem-sym-1", quantity=10, limit_price=100.0)
    intent2 = _intent(strategy_id="DoubleStraddelAlgo", symbol="NIFTY", idempotency_key="idem-sym-2", quantity=10, limit_price=100.0)
    d1 = mgr1.evaluate_and_reserve(intent1, strategy_id="CombinedVwapNifty", account_id=ACCOUNT_A)
    d2 = mgr1.evaluate_and_reserve(intent2, strategy_id="DoubleStraddelAlgo", account_id=ACCOUNT_A)
    mgr1.commit_reservation(d1.reservation_id, execution_result=ExecutionResult(success=True, status="COMPLETE"))
    mgr1.commit_reservation(d2.reservation_id, execution_result=ExecutionResult(success=True, status="COMPLETE"))
    del mgr1

    mgr2 = _manager(tmp_path)
    cv = mgr2.get_snapshot(strategy_id="CombinedVwapNifty", account_id=ACCOUNT_A)
    ds = mgr2.get_snapshot(strategy_id="DoubleStraddelAlgo", account_id=ACCOUNT_A)
    assert cv.strategy_exposure == 1000.0
    assert ds.strategy_exposure == 1000.0
    assert cv.account_exposure == 2000.0  # additive, neither overwrote the other's ledger row (different strategy_id in the key)


# --------------------------------------------------------------------------- #
# Write-failure / transaction-failure fail-closed (Section 59-60)
# --------------------------------------------------------------------------- #
def test_reservation_write_failure_fails_closed_no_execution(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)

    def _boom(*a, **kw):
        raise OSError("simulated disk write failure")

    monkeypatch.setattr(mgr._store, "create_reservation", _boom)
    decision = mgr.evaluate_and_reserve(_intent(idempotency_key="idem-write-fail"), strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert decision.allowed is False
    assert decision.reason_code == "INVALID_RISK_STATE"
    # No in-memory reservation was created either -- the whole locked
    # block raised before touching self._reservations.
    assert len(mgr._reservations) == 0


# --------------------------------------------------------------------------- #
# WorkerCoordinator-level fail-closed gate (Section 24) -- proves the
# NOT_READY rejection propagates all the way through the SAME distributed
# path a real worker submission uses, not just the direct PortfolioRiskManager API.
# --------------------------------------------------------------------------- #
def test_worker_coordinator_rejects_when_portfolio_risk_not_ready(tmp_path):
    from trading.common.broker import OrderType
    from trading.common.broker_manager import BrokerManager
    from trading.common.brokers.paper_broker import PaperBroker
    from trading.common.kill_switch import CentralKillSwitch
    from trading.common.risk_manager import RiskManager
    from trading.common.strategy import BaseStrategy
    from trading.common.strategy_assignment import StrategyAssignment
    from trading.common.strategy_registry import StrategyRegistry
    from trading.common.strategy_runtime import StrategyRuntime
    from trading.common.trading_account import ExecutionMode, TradingAccount
    from trading.common.worker_coordinator import WorkerCoordinator
    from trading.common.worker_protocol import OrderIntentSubmission
    from trading.common.worker_registry import WorkerRegistry

    class _OneShotStrategy(BaseStrategy):
        def _on_generate_order_intents(self):
            return []

    open_or_diagnose(_db(tmp_path))  # first run
    Path(_db(tmp_path)).unlink()  # unexpected loss
    store, readiness, _ = open_or_diagnose(_db(tmp_path))
    assert readiness == PortfolioRiskReadiness.NOT_READY
    prm = PortfolioRiskManager(store=store, readiness=readiness)

    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id="ACC1", account_name="A", broker_id="paper", execution_mode=ExecutionMode.SHADOW),
        broker_client=PaperBroker(),
    )
    registry = StrategyRegistry()
    strategy = _OneShotStrategy("StrategyA")
    registry.register(strategy)
    assignment = StrategyAssignment(manager)
    assignment.assign("StrategyA", "ACC1")
    registry.enable("StrategyA")
    registry.start("StrategyA")
    risk_manager = RiskManager(assignment)
    kill_switch = CentralKillSwitch()
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch,
    )
    workers = WorkerRegistry()
    coordinator = WorkerCoordinator(
        worker_registry=workers, strategy_registry=registry, strategy_assignment=assignment,
        strategy_runtime=runtime, portfolio_risk_manager=prm,
    )
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy("StrategyA", "w1")

    intent = OrderIntent(
        strategy_id="StrategyA", account_id="ACC1", symbol="NIFTY", exchange="NFO",
        side=OrderSide.BUY, quantity=1, order_type=OrderType.MARKET, idempotency_key="k1",
    )
    result = coordinator.submit_order_intent(OrderIntentSubmission(
        worker_id="w1", session_id=worker.session_id, strategy_id="StrategyA", evaluation_id="e1", intent=intent,
    ))
    assert result.accepted is False
    assert "RISK_NOT_READY" in result.reason
    assert result.execution_result is None  # never reached execute_worker_intent -- 0 broker mutation attempts
