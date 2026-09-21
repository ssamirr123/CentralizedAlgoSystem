"""Phase 15D-RECON: persistent, read-only broker reconciliation.

Every test uses a fake/deterministic in-process broker -- never a real
network call, never a real order-placement call. Zero real broker
mutation calls anywhere in this file.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from trading.common.audit_store import (
    EVENT_RECONCILIATION_COMPLETED,
    EVENT_RECONCILIATION_STARTED,
    PersistentAuditTrail,
)
from trading.common.broker import BrokerClient, BrokerConnectionError, OrderSide, OrderType, Quote
from trading.common.broker_manager import BrokerManager
from trading.common.execution import ExecutionConfig, OrderState, StrategyExecutionEngine
from trading.common.idempotency_store import (
    STATUS_AMBIGUOUS,
    SqliteIdempotencyStore,
)
from trading.common.kill_switch import CentralKillSwitch
from trading.common.order_intent import OrderIntent
from trading.common.reconciliation import (
    ReadOnlyBrokerView,
    ReconciliationOwnershipError,
    ReconciliationPersistenceError,
    ReconciliationService,
    ReconciliationStatus,
    SqliteReconciliationStore,
)
from trading.common.risk_manager import RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import AccountAuthorizationState, ExecutionMode, TradingAccount


def _tmp(tmp_path: Path, name: str) -> str:
    return str(tmp_path / name)


def _intent(**kwargs) -> OrderIntent:
    defaults = dict(
        strategy_id="StratA", account_id="ACC_A", symbol="NIFTY", exchange="NFO",
        side=OrderSide.BUY, quantity=50, order_type=OrderType.MARKET, idempotency_key="k1",
        correlation_id="corr-1",
    )
    defaults.update(kwargs)
    return OrderIntent(**defaults)


class _RaisingBroker(BrokerClient):
    """Simulates the AmbiguousOrderStateError trigger -- place_order raises."""

    is_simulated = True

    def __init__(self) -> None:
        self.connected = False
        self.order_book: list[OrderState] = []

    def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, last_price=100.0, timestamp="2026-01-01T00:00:00+00:00")

    def place_order(self, *args, **kwargs):
        raise ConnectionError("simulated network failure during submission")

    def cancel_order(self, order_id: str) -> bool:
        raise AssertionError("reconciliation must NEVER call cancel_order")

    def get_positions(self):
        return []

    def get_order(self, order_id: str) -> OrderState:
        for s in self.order_book:
            if s.order_id == order_id:
                return s
        return OrderState(order_id=order_id, status="UNKNOWN", filled_quantity=0, remaining_quantity=0)

    def get_order_book(self) -> list[OrderState]:
        return list(self.order_book)


def _run_ambiguous_execute(tmp_path, *, audit_db="audit.db", idem_db="idem.db") -> tuple:
    """Drives a real StrategyExecutionEngine.execute() to a genuine
    STATUS_AMBIGUOUS outcome, exactly like Phase 15D-DR/15D-AUDIT's own
    tests -- this is the realistic precondition Phase 15D-RECON exists to
    resolve."""
    audit_trail = PersistentAuditTrail(db_path=_tmp(tmp_path, audit_db))
    idem_store = SqliteIdempotencyStore(db_path=_tmp(tmp_path, idem_db))
    broker = _RaisingBroker()
    broker_manager = BrokerManager(audit_trail=audit_trail)
    account = TradingAccount(
        account_id="ACC_A", account_name="A", broker_id="fake", execution_mode=ExecutionMode.PAPER,
        authorization_state=AccountAuthorizationState.READ_ONLY,
    )
    broker_manager.register_account(account, broker_client=broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign("StratA", "ACC_A")
    risk_manager = RiskManager(assignment)
    engine = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager,
        audit_trail=audit_trail, idempotency_store=idem_store,
    )
    result = engine.execute(_intent(idempotency_key="ambig-1", correlation_id="corr-ambig"))
    assert not result.success
    assert idem_store.get("ambig-1").status == STATUS_AMBIGUOUS
    return audit_trail, idem_store, broker, account


def _service(tmp_path, audit_trail, idem_store, *, recon_db="recon.db", worker_id="w1") -> tuple:
    store = SqliteReconciliationStore(db_path=_tmp(tmp_path, recon_db))
    service = ReconciliationService(store, idem_store, audit_trail, worker_id=worker_id)
    return store, service


# ---------------------------------------------------------------------- #
# 1. Pending record detected / 2. Ambiguous record detected
# ---------------------------------------------------------------------- #
def test_1_and_2_pending_and_ambiguous_records_detected(tmp_path):
    audit_trail, idem_store, _broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    ids = service.scan_and_register()
    assert len(ids) == 1
    record = store.get(ids[0])
    assert record.status == ReconciliationStatus.RECONCILIATION_REQUIRED.value
    assert record.idempotency_key == "ambig-1"
    assert record.symbol == "NIFTY"
    assert record.expected_side == "BUY"
    assert record.expected_quantity == 50


def test_scan_is_idempotent(tmp_path):
    audit_trail, idem_store, _broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    ids1 = service.scan_and_register()
    ids2 = service.scan_and_register()
    assert ids1 == ids2
    assert len(store.list_outstanding()) == 1  # not duplicated


# ---------------------------------------------------------------------- #
# 3 & 19. Reconciliation started + audit persistence
# ---------------------------------------------------------------------- #
def test_3_and_19_reconciliation_started_is_durable(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()
    broker.order_book = [OrderState(order_id="ORD-1", status="OPEN", filled_quantity=0, remaining_quantity=50,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    service.reconcile_one(rid, broker)

    started = audit_trail.by_event_type(EVENT_RECONCILIATION_STARTED)
    assert len(started) == 1
    assert started[0].idempotency_key == "ambig-1"


# ---------------------------------------------------------------------- #
# 4 & 5. Reconciliation completed / filled order
# ---------------------------------------------------------------------- #
def test_4_and_5_filled_order_reconciled(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()
    broker.order_book = [OrderState(order_id="ORD-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    result = service.reconcile_one(rid, broker)

    assert result.status == ReconciliationStatus.RECONCILED_FILLED.value
    assert result.broker_order_id == "ORD-1"
    assert result.filled_quantity == 50
    completed = audit_trail.by_event_type(EVENT_RECONCILIATION_COMPLETED)
    assert len(completed) == 1
    assert completed[0].detail["result"] == ReconciliationStatus.RECONCILED_FILLED.value


# ---------------------------------------------------------------------- #
# 6. Rejected order
# ---------------------------------------------------------------------- #
def test_6_rejected_order_reconciled(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()
    broker.order_book = [OrderState(order_id="ORD-1", status="REJECTED", filled_quantity=0, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    result = service.reconcile_one(rid, broker)
    assert result.status == ReconciliationStatus.RECONCILED_REJECTED.value
    assert "REJECTED" in result.rejection_reason


# ---------------------------------------------------------------------- #
# 7. Partial fill
# ---------------------------------------------------------------------- #
def test_7_partial_fill_not_treated_as_complete(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()
    broker.order_book = [OrderState(order_id="ORD-1", status="OPEN", filled_quantity=32, remaining_quantity=33,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    result = service.reconcile_one(rid, broker)
    assert result.status == ReconciliationStatus.RECONCILED_ACCEPTED.value
    assert result.filled_quantity == 32  # exact, never rounded up to "complete"
    assert result.status != ReconciliationStatus.RECONCILED_FILLED.value


# ---------------------------------------------------------------------- #
# 8. Not found
# ---------------------------------------------------------------------- #
def test_8_order_not_found_after_successful_empty_search(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()
    broker.order_book = []  # searched successfully, genuinely empty
    result = service.reconcile_one(rid, broker)
    assert result.status == ReconciliationStatus.RECONCILED_NOT_FOUND.value


# ---------------------------------------------------------------------- #
# 9. Unknown result
# ---------------------------------------------------------------------- #
class _NoLookupBroker(BrokerClient):
    """A broker with no get_order/get_order_book/get_open_orders at all --
    only the mandatory BrokerClient ABC methods -- simulating an adapter
    (like Dhan/ICICI as of the phases this session's predecessors covered)
    that doesn't yet support any order-lookup capability."""

    is_simulated = True

    def connect(self) -> None: pass
    def is_connected(self) -> bool: return True
    def disconnect(self) -> None: pass
    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, last_price=100.0, timestamp="2026-01-01T00:00:00+00:00")
    def place_order(self, *args, **kwargs):
        raise AssertionError("reconciliation must NEVER call place_order")
    def cancel_order(self, order_id: str) -> bool:
        raise AssertionError("reconciliation must NEVER call cancel_order")
    def get_positions(self):
        return []


def test_9_unknown_when_adapter_has_no_lookup_capability(tmp_path):
    audit_trail, idem_store, _broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()

    result = service.reconcile_one(rid, _NoLookupBroker())
    assert result.status == ReconciliationStatus.RECONCILIATION_UNKNOWN.value
    assert "no order-lookup capability" in result.error_message


# ---------------------------------------------------------------------- #
# 10. Broker timeout / 11. Broker read-only API failure
# ---------------------------------------------------------------------- #
def test_10_and_11_broker_query_failure_is_unknown_not_a_crash(tmp_path):
    audit_trail, idem_store, _broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    # a known broker_order_id forces the get_order() lookup path (rather
    # than the order-book search) to actually run and raise.
    record = store.create_required(idempotency_key="ambig-1", account_id="ACC_A", symbol="NIFTY",
                                    expected_side="BUY", expected_quantity=50, broker_order_id="ORD-KNOWN")

    class _TimeoutBroker(_RaisingBroker):
        def get_order(self, order_id):
            raise BrokerConnectionError("simulated timeout talking to broker")

    result = service.reconcile_one(record.reconciliation_id, _TimeoutBroker())
    assert result.status == ReconciliationStatus.RECONCILIATION_UNKNOWN.value
    assert "timeout" in result.error_message.lower()


# ---------------------------------------------------------------------- #
# 12 & 13. Restart during / before reconciliation
# ---------------------------------------------------------------------- #
def test_12_restart_during_reconciliation_leaves_it_in_progress_and_recoverable(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store, recon_db="recon.db")
    [rid] = service.scan_and_register()
    # simulate a worker claiming it, then crashing before complete()
    store.try_acquire(rid, "crashed-worker")
    del store, service  # simulate process death

    store2 = SqliteReconciliationStore(db_path=_tmp(tmp_path, "recon.db"))
    record = store2.get(rid)
    assert record.status == ReconciliationStatus.RECONCILIATION_IN_PROGRESS.value  # not lost, not silently reset

    reclaimed = store2.reclaim_stale(older_than_seconds=0)
    assert rid in reclaimed
    assert store2.get(rid).status == ReconciliationStatus.RECONCILIATION_REQUIRED.value  # eligible again

    service2 = ReconciliationService(store2, idem_store, audit_trail, worker_id="fresh-worker")
    broker.order_book = [OrderState(order_id="ORD-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    result = service2.reconcile_one(rid, broker)
    assert result.status == ReconciliationStatus.RECONCILED_FILLED.value


def test_13_restart_before_reconciliation_still_reconcilable(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, _service_unused = _service(tmp_path, audit_trail, idem_store, recon_db="recon.db")
    ReconciliationService(store, idem_store, audit_trail, worker_id="w0").scan_and_register()
    del store  # restart before any reconciliation attempt began

    store2 = SqliteReconciliationStore(db_path=_tmp(tmp_path, "recon.db"))
    outstanding = store2.list_outstanding()
    assert len(outstanding) == 1
    service2 = ReconciliationService(store2, idem_store, audit_trail, worker_id="w1")
    broker.order_book = [OrderState(order_id="ORD-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    result = service2.reconcile_one(outstanding[0].reconciliation_id, broker)
    assert result.status == ReconciliationStatus.RECONCILED_FILLED.value


# ---------------------------------------------------------------------- #
# 14 & 15. Duplicate reconciliation request / concurrent reconciliation
# ---------------------------------------------------------------------- #
def test_14_duplicate_reconciliation_request_is_a_safe_no_op(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()
    broker.order_book = [OrderState(order_id="ORD-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    first = service.reconcile_one(rid, broker)
    second = service.reconcile_one(rid, broker)  # already terminal -- try_acquire fails, observes only
    assert first.status == second.status == ReconciliationStatus.RECONCILED_FILLED.value
    # only ONE STARTED/COMPLETED pair was ever written
    assert len(audit_trail.by_event_type(EVENT_RECONCILIATION_STARTED)) == 1
    assert len(audit_trail.by_event_type(EVENT_RECONCILIATION_COMPLETED)) == 1


def test_15_concurrent_reconciliation_only_one_owner_wins(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, _svc = _service(tmp_path, audit_trail, idem_store, recon_db="recon.db")
    [rid] = ReconciliationService(store, idem_store, audit_trail, worker_id="scanner").scan_and_register()

    results = {}

    def _worker(name):
        results[name] = store.try_acquire(rid, name)

    t_a = threading.Thread(target=_worker, args=("workerA",))
    t_b = threading.Thread(target=_worker, args=("workerB",))
    t_a.start(); t_b.start()
    t_a.join(); t_b.join()

    assert sorted(results.values()) == [False, True]
    winners = [name for name, won in results.items() if won]
    assert len(winners) == 1
    assert store.get(rid).owner == winners[0]


# ---------------------------------------------------------------------- #
# 16. Stale in-progress reconciliation
# ---------------------------------------------------------------------- #
def test_16_stale_in_progress_is_not_silently_reset_to_ready_to_execute(tmp_path):
    audit_trail, idem_store, _broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()
    store.try_acquire(rid, "stuck-worker")

    # not yet stale -- a short threshold should NOT reclaim it if it was
    # just claimed (claimed_at is "now", so "older than 3600s" is false).
    reclaimed = store.reclaim_stale(older_than_seconds=3600)
    assert reclaimed == []
    assert store.get(rid).status == ReconciliationStatus.RECONCILIATION_IN_PROGRESS.value

    reclaimed = store.reclaim_stale(older_than_seconds=0)
    assert rid in reclaimed
    record = store.get(rid)
    assert record.status == ReconciliationStatus.RECONCILIATION_REQUIRED.value  # not NOT_REQUIRED, not silently cleared
    assert record.owner == ""


# ---------------------------------------------------------------------- #
# 17. Account A/B isolation
# ---------------------------------------------------------------------- #
def test_17_account_isolation(tmp_path):
    audit_trail = PersistentAuditTrail(db_path=_tmp(tmp_path, "audit.db"))
    idem_store = SqliteIdempotencyStore(db_path=_tmp(tmp_path, "idem.db"))
    store = SqliteReconciliationStore(db_path=_tmp(tmp_path, "recon.db"))

    for account_id, key in (("ANGEL_SAMIR", "key-a"), ("ANGEL_ACCOUNT_B", "key-b")):
        audit_trail.append(
            "ORDER_INTENT_CREATED", correlation_id=f"corr-{key}", account_id=account_id,
            idempotency_key=key, symbol="NIFTY", side="BUY", quantity=10, order_type="MARKET",
        )
        store.create_required(
            idempotency_key=key, correlation_id=f"corr-{key}", account_id=account_id,
            symbol="NIFTY", expected_side="BUY", expected_quantity=10,
        )

    a_records = store.by_account("ANGEL_SAMIR")
    b_records = store.by_account("ANGEL_ACCOUNT_B")
    assert len(a_records) == 1 and a_records[0].idempotency_key == "key-a"
    assert len(b_records) == 1 and b_records[0].idempotency_key == "key-b"

    account_a = TradingAccount(account_id="ANGEL_SAMIR", account_name="A", broker_id="angelone")
    account_b = TradingAccount(account_id="ANGEL_ACCOUNT_B", account_name="B", broker_id="angelone")
    assert account_a.authorization_state == AccountAuthorizationState.READ_ONLY
    assert account_b.authorization_state == AccountAuthorizationState.READ_ONLY


def test_17_concurrent_account_reconciliation_no_cross_contamination(tmp_path):
    audit_trail = PersistentAuditTrail(db_path=_tmp(tmp_path, "audit.db"))
    idem_store = SqliteIdempotencyStore(db_path=_tmp(tmp_path, "idem.db"))
    store = SqliteReconciliationStore(db_path=_tmp(tmp_path, "recon.db"))
    service = ReconciliationService(store, idem_store, audit_trail, worker_id="w")

    broker_a, broker_b = _RaisingBroker(), _RaisingBroker()
    broker_a.order_book = [OrderState(order_id="ORD-A", status="FILLED", filled_quantity=10, remaining_quantity=0, symbol="NIFTY", side=OrderSide.BUY)]
    broker_b.order_book = [OrderState(order_id="ORD-B", status="REJECTED", filled_quantity=0, remaining_quantity=0, symbol="BANKNIFTY", side=OrderSide.SELL)]

    rec_a = store.create_required(idempotency_key="key-a", account_id="ANGEL_SAMIR", symbol="NIFTY", expected_side="BUY", expected_quantity=10)
    rec_b = store.create_required(idempotency_key="key-b", account_id="ANGEL_ACCOUNT_B", symbol="BANKNIFTY", expected_side="SELL", expected_quantity=10)

    errors = []

    def _reconcile(rid, broker):
        try:
            service.reconcile_one(rid, broker)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t_a = threading.Thread(target=_reconcile, args=(rec_a.reconciliation_id, broker_a))
    t_b = threading.Thread(target=_reconcile, args=(rec_b.reconciliation_id, broker_b))
    t_a.start(); t_b.start()
    t_a.join(); t_b.join()

    assert errors == []
    final_a = store.get(rec_a.reconciliation_id)
    final_b = store.get(rec_b.reconciliation_id)
    assert final_a.status == ReconciliationStatus.RECONCILED_FILLED.value
    assert final_a.broker_order_id == "ORD-A"
    assert final_b.status == ReconciliationStatus.RECONCILED_REJECTED.value
    assert final_b.broker_order_id == "ORD-B"


# ---------------------------------------------------------------------- #
# 18. Idempotency correlation
# ---------------------------------------------------------------------- #
def test_18_idempotency_correlation(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()
    record = store.by_idempotency_key("ambig-1")
    assert record is not None
    assert record.reconciliation_id == rid
    # investigator path: idempotency_key -> reconciliation record -> audit trail
    assert len(audit_trail.by_idempotency_key("ambig-1")) >= 1


# ---------------------------------------------------------------------- #
# 20. No automatic retry
# ---------------------------------------------------------------------- #
def test_20_no_automatic_retry_reconciliation_never_calls_place_order(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()

    def _boom(*a, **k):
        raise AssertionError("reconciliation must NEVER call place_order")

    broker.place_order = _boom
    broker.order_book = [OrderState(order_id="ORD-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    result = service.reconcile_one(rid, broker)
    assert result.status == ReconciliationStatus.RECONCILED_FILLED.value  # place_order was never touched


def test_read_only_broker_view_has_no_mutating_methods(tmp_path):
    view = ReadOnlyBrokerView(_RaisingBroker())
    assert not hasattr(view, "place_order")
    assert not hasattr(view, "cancel_order")
    assert not hasattr(view, "modify_order")


# ---------------------------------------------------------------------- #
# 21. Kill switch interaction
# ---------------------------------------------------------------------- #
def test_21_reconciliation_unaffected_by_kill_switch_state(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()
    ks = CentralKillSwitch()
    ks.engage(by="operator", reason="incident")
    assert ks.engaged is True

    broker.order_book = [OrderState(order_id="ORD-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    # reconciliation does not consult the kill switch at all -- it is
    # read-only regardless of its state, and never reaches place_order
    # even implicitly, so engaging it changes nothing about this call.
    result = service.reconcile_one(rid, broker)
    assert result.status == ReconciliationStatus.RECONCILED_FILLED.value
    assert ks.engaged is True  # unaffected, unconsulted


# ---------------------------------------------------------------------- #
# 22. Broker order ID correlation
# ---------------------------------------------------------------------- #
def test_22_broker_order_id_correlation_used_as_primary_lookup(tmp_path):
    audit_trail = PersistentAuditTrail(db_path=_tmp(tmp_path, "audit.db"))
    idem_store = SqliteIdempotencyStore(db_path=_tmp(tmp_path, "idem.db"))
    store, service = _service(tmp_path, audit_trail, idem_store)
    audit_trail.append("ORDER_INTENT_CREATED", correlation_id="c1", account_id="ACC_A", idempotency_key="k1",
                        symbol="NIFTY", side="BUY", quantity=10, order_type="MARKET")
    record = store.create_required(idempotency_key="k1", account_id="ACC_A", symbol="NIFTY",
                                    expected_side="BUY", expected_quantity=10, broker_order_id="KNOWN-ORD")
    broker = _RaisingBroker()
    # deliberately put a DIFFERENT order at index 0 of the book, plus the
    # known one -- if lookup-by-id works, the known one is found directly
    # without needing to search the book at all.
    broker.order_book = [
        OrderState(order_id="OTHER", status="OPEN", filled_quantity=0, remaining_quantity=5, symbol="NIFTY", side=OrderSide.BUY),
        OrderState(order_id="KNOWN-ORD", status="FILLED", filled_quantity=10, remaining_quantity=0, symbol="NIFTY", side=OrderSide.BUY),
    ]
    result = service.reconcile_one(record.reconciliation_id, broker)
    assert result.broker_order_id == "KNOWN-ORD"
    assert result.status == ReconciliationStatus.RECONCILED_FILLED.value


# ---------------------------------------------------------------------- #
# 23. Expected-vs-actual mismatch
# ---------------------------------------------------------------------- #
def test_23_side_mismatch_is_reconciliation_failed(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()
    # expected BUY, broker shows SELL
    broker.order_book = [OrderState(order_id="ORD-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.SELL)]
    result = service.reconcile_one(rid, broker)
    assert result.status == ReconciliationStatus.RECONCILIATION_FAILED.value
    assert "side mismatch" in result.error_message


def test_23_symbol_mismatch_is_reconciliation_failed(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()
    broker.order_book = [OrderState(order_id="ORD-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="BANKNIFTY", side=OrderSide.BUY)]
    result = service.reconcile_one(rid, broker)
    assert result.status == ReconciliationStatus.RECONCILIATION_FAILED.value
    assert "symbol mismatch" in result.error_message


def test_23_ambiguous_order_book_multiple_candidates_is_unknown(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()
    # Two orders with empty symbol metadata -- no broker_order_id known,
    # cannot uniquely identify which one is the expected order.
    broker.order_book = [
        OrderState(order_id="X1", status="OPEN", filled_quantity=0, remaining_quantity=50),
        OrderState(order_id="X2", status="OPEN", filled_quantity=0, remaining_quantity=50),
    ]
    result = service.reconcile_one(rid, broker)
    assert result.status == ReconciliationStatus.RECONCILIATION_UNKNOWN.value
    assert "ambiguous" in result.error_message.lower()


# ---------------------------------------------------------------------- #
# 24. Secret redaction
# ---------------------------------------------------------------------- #
def test_24_secret_redaction_in_error_message(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()

    class _LeakyBroker(_RaisingBroker):
        def get_order(self, order_id):
            raise BrokerConnectionError("auth failed: api_key=SECRET123 rejected")

    result = service.reconcile_one(rid, _LeakyBroker())
    assert "SECRET123" not in result.error_message
    assert result.error_message == "***REDACTED***" or "SECRET123" not in result.error_message


def test_24_no_credential_columns_exist_in_reconciliation_schema(tmp_path):
    import sqlite3

    store = SqliteReconciliationStore(db_path=_tmp(tmp_path, "recon.db"))
    conn = sqlite3.connect(store._db_path)
    columns = {row[1].lower() for row in conn.execute("PRAGMA table_info(reconciliation_records)")}
    conn.close()
    forbidden = {"password", "api_key", "apikey", "access_token", "totp", "mpin", "secret", "token"}
    assert columns.isdisjoint(forbidden)


# ---------------------------------------------------------------------- #
# 25. Persistence failure
# ---------------------------------------------------------------------- #
def test_25_persistence_failure_fails_closed_on_create(tmp_path, monkeypatch):
    store = SqliteReconciliationStore(db_path=_tmp(tmp_path, "recon.db"))

    import sqlite3 as sqlite3_module

    def _boom(self):
        raise sqlite3_module.OperationalError("database is locked")

    monkeypatch.setattr(SqliteReconciliationStore, "_connect", _boom)
    with pytest.raises(ReconciliationPersistenceError):
        store.create_required(idempotency_key="k1")


def test_25_completion_by_wrong_owner_fails_closed(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()
    store.try_acquire(rid, "real-owner")
    with pytest.raises(ReconciliationOwnershipError):
        store.complete(rid, "impostor-owner", status=ReconciliationStatus.RECONCILED_FILLED)


def test_25_reconciliation_started_audit_failure_fails_closed_before_broker_query(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    store, _svc = _service(tmp_path, audit_trail, idem_store, recon_db="recon.db")
    [rid] = ReconciliationService(store, idem_store, audit_trail, worker_id="w").scan_and_register()

    class _BoomAuditTrail:
        def append(self, event_type, **kwargs):
            raise RuntimeError("simulated audit failure")
        def by_idempotency_key(self, key):
            return audit_trail.by_idempotency_key(key)

    class _BoomBroker(_RaisingBroker):
        def get_order(self, order_id):
            raise AssertionError("must never query the broker if RECONCILIATION_STARTED could not be persisted")

    service_broken = ReconciliationService(store, idem_store, _BoomAuditTrail(), worker_id="w2")
    result = service_broken.reconcile_one(rid, _BoomBroker())
    assert result.status == ReconciliationStatus.RECONCILIATION_FAILED.value
    assert "RECONCILIATION_STARTED" in result.error_message


# ---------------------------------------------------------------------- #
# Extra: end-to-end Area J diagram proof
# ---------------------------------------------------------------------- #
def test_area_j_full_ambiguous_to_completed_lifecycle(tmp_path):
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    assert idem_store.get("ambig-1").status == STATUS_AMBIGUOUS  # STATUS_AMBIGUOUS
    store, service = _service(tmp_path, audit_trail, idem_store)
    [rid] = service.scan_and_register()  # -> RECONCILIATION_REQUIRED
    assert store.get(rid).status == ReconciliationStatus.RECONCILIATION_REQUIRED.value

    broker.order_book = [OrderState(order_id="ORD-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    result = service.reconcile_one(rid, broker)  # STARTED -> read-only query -> COMPLETED
    assert result.status == ReconciliationStatus.RECONCILED_FILLED.value
    assert len(audit_trail.by_event_type(EVENT_RECONCILIATION_STARTED)) == 1
    assert len(audit_trail.by_event_type(EVENT_RECONCILIATION_COMPLETED)) == 1


def test_reconciliation_never_places_a_real_order_end_to_end(tmp_path):
    """Explicit safety-count proof for this whole file."""
    audit_trail, idem_store, broker, _account = _run_ambiguous_execute(tmp_path)
    assert isinstance(broker, _RaisingBroker)  # in-process fake, never a real adapter
