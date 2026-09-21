"""Phase 17.1-R Remediation E: feeding a terminal reconciliation result
back into the authoritative IdempotencyStore, and (when wired) into
PortfolioRiskManager's held reservation.

Reuses tests/common/test_phase_15d_recon.py's own `_run_ambiguous_execute`/
`_service` helpers to drive a real STATUS_AMBIGUOUS outcome exactly like
that file already does -- no real broker anywhere in this file either.
"""
from __future__ import annotations

import threading
from pathlib import Path

from trading.common.broker import OrderSide, OrderType
from trading.common.execution import ExecutionResult
from trading.common.idempotency_store import STATUS_AMBIGUOUS, STATUS_COMPLETED, STATUS_REJECTED
from trading.common.order_intent import OrderIntent
from trading.common.portfolio_risk import PortfolioRiskLimits, PortfolioRiskManager
from trading.common.reconciliation import ReconciliationService, ReconciliationStatus, SqliteReconciliationStore
from trading.common.execution import OrderState

from tests.common.test_phase_15d_recon import _run_ambiguous_execute, _service


def _reconciled_intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id="StratA", account_id="ACC_A", symbol="NIFTY", exchange="NFO",
        side=OrderSide.BUY, quantity=50, order_type=OrderType.MARKET, idempotency_key="ambig-1",
    )
    fields.update(overrides)
    return OrderIntent(**fields)


def test_reconciled_filled_transitions_idempotency_to_completed(tmp_path: Path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    assert idem_store.get("ambig-1").status == STATUS_AMBIGUOUS
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()

    broker.order_book = [OrderState(order_id="ORD-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    result = service.reconcile_one(rid, broker)
    assert result.status == ReconciliationStatus.RECONCILED_FILLED.value

    record = idem_store.get("ambig-1")
    assert record.status == STATUS_COMPLETED
    assert record.broker_order_id == "ORD-1"

    # A future retry of this exact key must replay the resolved COMPLETED
    # result, never call the broker again -- prove via execute() itself.
    from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
    from trading.common.risk_manager import RiskManager
    from trading.common.strategy_assignment import StrategyAssignment
    from trading.common.broker_manager import BrokerManager
    broker_manager = BrokerManager(audit_trail=audit_trail)
    broker_manager.register_account(_account, broker_client=broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign("StratA", "ACC_A")
    engine = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0),
        risk_manager=RiskManager(assignment), strategy_assignment=assignment, broker_manager=broker_manager,
        audit_trail=audit_trail, idempotency_store=idem_store,
    )

    broker.place_order = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not call broker again"))

    replay = engine.execute(_reconciled_intent())
    assert replay.success is True
    assert replay.order_id == "ORD-1"


def test_reconciled_not_found_transitions_idempotency_to_rejected(tmp_path: Path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()

    broker.order_book = []
    result = service.reconcile_one(rid, broker)
    assert result.status == ReconciliationStatus.RECONCILED_NOT_FOUND.value

    record = idem_store.get("ambig-1")
    assert record.status == STATUS_REJECTED

    # Section 23 fail-closed policy: a NOT_FOUND resolution must never
    # authorize a fresh retry of the SAME key either.
    from trading.common.execution import AmbiguousOrderStateError, ExecutionConfig, StrategyExecutionEngine
    from trading.common.risk_manager import RiskManager
    from trading.common.strategy_assignment import StrategyAssignment
    from trading.common.broker_manager import BrokerManager
    broker_manager = BrokerManager(audit_trail=audit_trail)
    broker_manager.register_account(_account, broker_client=broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign("StratA", "ACC_A")
    engine = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0),
        risk_manager=RiskManager(assignment), strategy_assignment=assignment, broker_manager=broker_manager,
        audit_trail=audit_trail, idempotency_store=idem_store,
    )
    replay = engine.execute(_reconciled_intent())
    assert replay.success is False  # replayed rejection, not a new attempt


def test_unresolved_reconciliation_leaves_idempotency_untouched(tmp_path: Path):
    """RECONCILIATION_UNKNOWN ("evidence insufficient") must HOLD, never
    guess FOUND or NOT_FOUND."""
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()

    # Force RECONCILIATION_UNKNOWN by making the order-book lookup itself raise.
    import trading.common.reconciliation as recon_mod

    class _FailingView(recon_mod.ReadOnlyBrokerView):
        def get_order_book(self):
            raise ConnectionError("order book query failed")

        def get_open_orders(self):
            raise ConnectionError("open orders query failed")

    original_view_cls = recon_mod.ReadOnlyBrokerView
    recon_mod.ReadOnlyBrokerView = _FailingView
    try:
        result = service.reconcile_one(rid, broker)
    finally:
        recon_mod.ReadOnlyBrokerView = original_view_cls

    assert result.status == ReconciliationStatus.RECONCILIATION_UNKNOWN.value
    record = idem_store.get("ambig-1")
    assert record.status == STATUS_AMBIGUOUS  # untouched -- still requires operator review


def test_resolve_ambiguous_is_a_noop_when_record_already_resolved(tmp_path: Path):
    """Concurrency safety: a second reconciliation attempt for a key
    already resolved must never double-apply the feedback."""
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()
    broker.order_book = [OrderState(order_id="ORD-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    service.reconcile_one(rid, broker)
    assert idem_store.get("ambig-1").status == STATUS_COMPLETED

    resolved_again = idem_store.resolve_ambiguous("ambig-1", new_status=STATUS_REJECTED)
    assert resolved_again is False  # already COMPLETED, not AMBIGUOUS/PENDING -- refuses to overwrite
    assert idem_store.get("ambig-1").status == STATUS_COMPLETED  # unchanged


# --------------------------------------------------------------------------- #
# Portfolio-risk reservation feedback (Section 24)
# --------------------------------------------------------------------------- #
def test_reconciliation_found_commits_the_held_portfolio_reservation(tmp_path: Path):
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=100_000.0))
    intent = _reconciled_intent(limit_price=100.0)
    decision = prm.evaluate_and_reserve(intent, strategy_id="StratA", account_id="ACC_A")
    reservation_id = decision.reservation_id
    prm.commit_reservation(reservation_id, execution_result=ExecutionResult.ambiguous(intent, "unknown"))
    snapshot_before = prm.get_snapshot(strategy_id="StratA", account_id="ACC_A")
    assert snapshot_before.strategy_exposure == 5000.0  # still held

    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    recon_store = SqliteReconciliationStore(db_path=str(tmp_path / "recon2.db"))
    service = ReconciliationService(recon_store, idem_store, audit_trail, worker_id="w1", portfolio_risk_manager=prm)
    [rid] = service.scan_and_register()
    broker.order_book = [OrderState(order_id="ORD-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    service.reconcile_one(rid, broker)

    snapshot_after = prm.get_snapshot(strategy_id="StratA", account_id="ACC_A")
    assert snapshot_after.strategy_exposure == 5000.0  # committed as a real position, not released
    assert prm.find_reservation_by_idempotency_key("ambig-1") is None  # resolved, no longer tracked


def test_reconciliation_not_found_releases_the_held_portfolio_reservation(tmp_path: Path):
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=100_000.0))
    intent = _reconciled_intent(limit_price=100.0)
    decision = prm.evaluate_and_reserve(intent, strategy_id="StratA", account_id="ACC_A")
    reservation_id = decision.reservation_id
    prm.commit_reservation(reservation_id, execution_result=ExecutionResult.ambiguous(intent, "unknown"))

    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    recon_store = SqliteReconciliationStore(db_path=str(tmp_path / "recon3.db"))
    service = ReconciliationService(recon_store, idem_store, audit_trail, worker_id="w1", portfolio_risk_manager=prm)
    [rid] = service.scan_and_register()
    broker.order_book = []
    service.reconcile_one(rid, broker)

    snapshot_after = prm.get_snapshot(strategy_id="StratA", account_id="ACC_A")
    assert snapshot_after.strategy_exposure == 0.0  # released -- never happened
    assert snapshot_after.strategy_orders_today == 0


def test_reconciliation_unresolved_keeps_portfolio_reservation_held(tmp_path: Path):
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=100_000.0))
    intent = _reconciled_intent(limit_price=100.0)
    decision = prm.evaluate_and_reserve(intent, strategy_id="StratA", account_id="ACC_A")
    reservation_id = decision.reservation_id
    prm.commit_reservation(reservation_id, execution_result=ExecutionResult.ambiguous(intent, "unknown"))

    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    recon_store = SqliteReconciliationStore(db_path=str(tmp_path / "recon4.db"))
    service = ReconciliationService(recon_store, idem_store, audit_trail, worker_id="w1", portfolio_risk_manager=prm)
    [rid] = service.scan_and_register()

    import trading.common.reconciliation as recon_mod

    class _FailingView(recon_mod.ReadOnlyBrokerView):
        def get_order_book(self):
            raise ConnectionError("order book query failed")

        def get_open_orders(self):
            raise ConnectionError("open orders query failed")

    original_view_cls = recon_mod.ReadOnlyBrokerView
    recon_mod.ReadOnlyBrokerView = _FailingView
    try:
        result = service.reconcile_one(rid, broker)
    finally:
        recon_mod.ReadOnlyBrokerView = original_view_cls

    assert result.status == ReconciliationStatus.RECONCILIATION_UNKNOWN.value
    snapshot_after = prm.get_snapshot(strategy_id="StratA", account_id="ACC_A")
    assert snapshot_after.strategy_exposure == 5000.0  # still held -- operator review required
    assert prm.find_reservation_by_idempotency_key("ambig-1") == reservation_id


# --------------------------------------------------------------------------- #
# TCC restart during ambiguity (Section 26)
# --------------------------------------------------------------------------- #
def test_ambiguous_state_and_reservation_survive_a_fresh_process_reopening_the_same_stores(tmp_path: Path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path, audit_db="restart_audit.db", idem_db="restart_idem.db")
    assert idem_store.get("ambig-1").status == STATUS_AMBIGUOUS

    # Simulate a TCC restart: brand-new IdempotencyStore instance pointed at
    # the SAME db file, never the same Python object.
    from trading.common.idempotency_store import SqliteIdempotencyStore
    reopened_idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / "restart_idem.db"))
    record = reopened_idem_store.get("ambig-1")
    assert record is not None
    assert record.status == STATUS_AMBIGUOUS  # survived restart, still unresolved

    # Reconciliation is still possible against the reopened store.
    store, service = _service(tmp_path, audit_trail, reopened_idem_store, recon_db="restart_recon.db")
    [rid] = service.scan_and_register()
    broker.order_book = [OrderState(order_id="ORD-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    result = service.reconcile_one(rid, broker)
    assert result.status == ReconciliationStatus.RECONCILED_FILLED.value
    assert reopened_idem_store.get("ambig-1").status == STATUS_COMPLETED


def test_two_concurrent_reconciliation_services_never_double_commit_or_double_release(tmp_path: Path):
    """Section 25: two 'processes' (here, two ReconciliationService
    instances sharing the SAME durable stores, exactly as two real worker
    processes would) racing to resolve the SAME ambiguous execution must
    produce exactly one authoritative resolution -- one idempotency
    transition, one portfolio-risk commit, and (implicitly, since neither
    ever calls a broker mutation method) zero duplicate broker calls."""
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=100_000.0))
    intent = _reconciled_intent(limit_price=100.0)
    decision = prm.evaluate_and_reserve(intent, strategy_id="StratA", account_id="ACC_A")
    prm.commit_reservation(decision.reservation_id, execution_result=ExecutionResult.ambiguous(intent, "unknown"))

    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path, audit_db="conc_audit.db", idem_db="conc_idem.db")
    recon_store = SqliteReconciliationStore(db_path=str(tmp_path / "conc_recon.db"))
    broker.order_book = [OrderState(order_id="ORD-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.BUY)]

    service_a = ReconciliationService(recon_store, idem_store, audit_trail, worker_id="procA", portfolio_risk_manager=prm)
    service_b = ReconciliationService(recon_store, idem_store, audit_trail, worker_id="procB", portfolio_risk_manager=prm)
    [rid] = service_a.scan_and_register()  # both processes would have scanned and found the same rid

    results = {}

    def _run(name, svc):
        results[name] = svc.reconcile_one(rid, broker)

    t_a = threading.Thread(target=_run, args=("A", service_a))
    t_b = threading.Thread(target=_run, args=("B", service_b))
    t_a.start(); t_b.start()
    t_a.join(); t_b.join()

    # Exactly one COMPLETED audit event -- the loser observes, never contends.
    from trading.common.audit_store import EVENT_RECONCILIATION_COMPLETED
    assert len(audit_trail.by_event_type(EVENT_RECONCILIATION_COMPLETED)) == 1

    assert idem_store.get("ambig-1").status == STATUS_COMPLETED
    snapshot = prm.get_snapshot(strategy_id="StratA", account_id="ACC_A")
    assert snapshot.strategy_exposure == 5000.0  # committed exactly once, never double-booked
    assert prm.find_reservation_by_idempotency_key("ambig-1") is None
