"""Phase 17.2-P Section 65-66: PortfolioRiskManager + PortfolioRiskStore +
IdempotencyStore + ReconciliationStore + ReconciliationService + the real
central execution path (StrategyExecutionEngine), end to end, with a
genuine TCC-restart simulation (fresh Python objects reopening the SAME
durable files) at each of the safety-critical crash points. No real
broker anywhere -- a fake, in-process BrokerClient double throughout,
exactly matching tests/common/test_phase_15d_recon.py's own established
pattern in this repo.
"""
from __future__ import annotations

from pathlib import Path

from trading.common.audit_store import PersistentAuditTrail
from trading.common.broker import BrokerClient, BrokerConnectionError, OrderSide, OrderType, Quote
from trading.common.broker_manager import BrokerManager
from trading.common.execution import ExecutionConfig, OrderState, StrategyExecutionEngine
from trading.common.idempotency_store import STATUS_AMBIGUOUS, STATUS_COMPLETED, SqliteIdempotencyStore
from trading.common.order_intent import OrderIntent
from trading.common.portfolio_risk import PortfolioRiskLimits, PortfolioRiskManager
from trading.common.portfolio_risk_store import PortfolioRiskReadiness, open_or_diagnose
from trading.common.reconciliation import ReconciliationService, ReconciliationStatus, SqliteReconciliationStore
from trading.common.risk_manager import RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import AccountAuthorizationState, ExecutionMode, TradingAccount

ACCOUNT_A = "ACC_A"
STRATEGY_A = "StratA"


class _FakeBroker(BrokerClient):
    """Records mutation ATTEMPTS only -- a fake, in-process double, never a
    real SDK/network call anywhere in this file."""

    is_simulated = True

    def __init__(self) -> None:
        self.connected = False
        self.order_book: list[OrderState] = []
        self.place_order_calls = 0
        self.raise_on_place = False

    def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, last_price=100.0, timestamp="2026-01-01T00:00:00+00:00")

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, limit_price=None):
        self.place_order_calls += 1
        if self.raise_on_place:
            raise ConnectionError("simulated network failure during submission")
        from trading.common.broker import OrderResult
        return OrderResult(order_id="FAKE-1", symbol=symbol, side=side, quantity=quantity, status="OPEN")

    def cancel_order(self, order_id: str) -> bool:
        raise AssertionError("this test file never calls cancel_order")

    def get_positions(self):
        return []

    def get_order(self, order_id: str) -> OrderState:
        for s in self.order_book:
            if s.order_id == order_id:
                return s
        return OrderState(order_id=order_id, status="UNKNOWN", filled_quantity=0, remaining_quantity=0)

    def get_order_book(self) -> list[OrderState]:
        return list(self.order_book)


def _intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id=STRATEGY_A, account_id=ACCOUNT_A, symbol="NIFTY", exchange="NFO",
        side=OrderSide.BUY, quantity=50, order_type=OrderType.LIMIT, limit_price=100.0,
        idempotency_key="e2e-key-1", correlation_id="corr-1",
    )
    fields.update(overrides)
    return OrderIntent(**fields)


def _stack(tmp_path: Path, *, portfolio_risk_manager, broker=None):
    audit_trail = PersistentAuditTrail(db_path=str(tmp_path / "audit.db"))
    idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / "idem.db"))
    broker = broker or _FakeBroker()
    broker_manager = BrokerManager(audit_trail=audit_trail)
    account = TradingAccount(
        account_id=ACCOUNT_A, account_name="A", broker_id="fake", execution_mode=ExecutionMode.PAPER,
        authorization_state=AccountAuthorizationState.READ_ONLY,
    )
    broker_manager.register_account(account, broker_client=broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign(STRATEGY_A, ACCOUNT_A)
    risk_manager = RiskManager(assignment)
    engine = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(max_retries=1, retry_delay_seconds=0),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager,
        audit_trail=audit_trail, idempotency_store=idem_store,
    )
    return audit_trail, idem_store, broker, engine


def _risk_manager(tmp_path, *, name="risk.db", idempotency_store=None) -> PortfolioRiskManager:
    store, readiness, detail = open_or_diagnose(str(tmp_path / name))
    assert readiness == PortfolioRiskReadiness.READY, detail
    return PortfolioRiskManager(
        portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=100_000.0),
        store=store, readiness=readiness, idempotency_store=idempotency_store,
    )


# --------------------------------------------------------------------------- #
# Confirmed success end-to-end
# --------------------------------------------------------------------------- #
def test_confirmed_success_reserves_and_commits_durably(tmp_path):
    prm = _risk_manager(tmp_path)
    audit_trail, idem_store, broker, engine = _stack(tmp_path, portfolio_risk_manager=prm)
    intent = _intent()

    decision = prm.evaluate_and_reserve(intent, strategy_id=STRATEGY_A, account_id=ACCOUNT_A)
    assert decision.allowed is True
    result = engine.execute(intent)
    assert result.success is True
    prm.commit_reservation(decision.reservation_id, execution_result=result)

    assert broker.place_order_calls == 1
    snapshot = prm.get_snapshot(strategy_id=STRATEGY_A, account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 5000.0


# --------------------------------------------------------------------------- #
# Risk rejection -- zero broker calls
# --------------------------------------------------------------------------- #
def test_portfolio_risk_rejection_never_reaches_broker(tmp_path):
    store, readiness, _ = open_or_diagnose(str(tmp_path / "risk.db"))
    prm = PortfolioRiskManager(
        portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=1.0),  # tiny -- guarantees rejection
        store=store, readiness=readiness,
    )
    audit_trail, idem_store, broker, engine = _stack(tmp_path, portfolio_risk_manager=prm)
    intent = _intent(idempotency_key="e2e-risk-reject")

    decision = prm.evaluate_and_reserve(intent, strategy_id=STRATEGY_A, account_id=ACCOUNT_A)
    assert decision.allowed is False
    assert broker.place_order_calls == 0  # execute() was never even called


# --------------------------------------------------------------------------- #
# AMBIGUOUS -> restart -> reconciliation FOUND -> restart again
# --------------------------------------------------------------------------- #
def test_ambiguous_then_restart_then_reconciliation_found_then_restart_again(tmp_path):
    prm1 = _risk_manager(tmp_path, idempotency_store=None)
    audit_trail, idem_store, broker, engine = _stack(tmp_path, portfolio_risk_manager=prm1)
    broker.raise_on_place = True
    intent = _intent(idempotency_key="e2e-ambig-1")

    decision = prm1.evaluate_and_reserve(intent, strategy_id=STRATEGY_A, account_id=ACCOUNT_A)
    result = engine.execute(intent)
    assert result.status == "AMBIGUOUS"
    prm1.commit_reservation(decision.reservation_id, execution_result=result)
    del prm1  # simulate TCC crash/restart -- no clean shutdown

    # Restart #1: idempotency has no COMPLETED/REJECTED record yet (the
    # broker call itself raised before any terminal outcome could be
    # persisted beyond AMBIGUOUS) -- must stay held.
    prm2 = _risk_manager(tmp_path, idempotency_store=idem_store)
    snapshot = prm2.get_snapshot(strategy_id=STRATEGY_A, account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 5000.0
    assert snapshot.strategy_open_orders == 1
    assert broker.place_order_calls == 1  # never retried across the restart

    # Now reconciliation runs and FINDS the order really did go through.
    recon_store = SqliteReconciliationStore(db_path=str(tmp_path / "recon.db"))
    service = ReconciliationService(recon_store, idem_store, audit_trail, worker_id="w1", portfolio_risk_manager=prm2)
    [rid] = service.scan_and_register()
    broker.order_book = [OrderState(order_id="FAKE-1", status="FILLED", filled_quantity=50, remaining_quantity=0,
                                     symbol="NIFTY", side=OrderSide.BUY)]
    outcome = service.reconcile_one(rid, broker)
    assert outcome.status == ReconciliationStatus.RECONCILED_FILLED.value
    assert idem_store.get("e2e-ambig-1").status == STATUS_COMPLETED

    snapshot = prm2.get_snapshot(strategy_id=STRATEGY_A, account_id=ACCOUNT_A)
    assert snapshot.strategy_open_orders == 0  # committed, no longer outstanding
    assert snapshot.strategy_exposure == 5000.0  # preserved as a real position

    del prm2  # crash again, right after reconciliation

    # Restart #2: the COMMITTED state (both idempotency AND portfolio risk)
    # must have been durable -- reopening from scratch must NOT show this
    # as outstanding/ambiguous again, and must NOT re-trigger any broker call.
    prm3 = _risk_manager(tmp_path, idempotency_store=idem_store)
    snapshot = prm3.get_snapshot(strategy_id=STRATEGY_A, account_id=ACCOUNT_A)
    assert snapshot.strategy_open_orders == 0
    assert snapshot.strategy_exposure == 5000.0
    assert broker.place_order_calls == 1  # still exactly once, ever


def test_ambiguous_then_reconciliation_not_found_then_restart(tmp_path):
    prm1 = _risk_manager(tmp_path)
    audit_trail, idem_store, broker, engine = _stack(tmp_path, portfolio_risk_manager=prm1)
    broker.raise_on_place = True
    intent = _intent(idempotency_key="e2e-notfound-1")

    decision = prm1.evaluate_and_reserve(intent, strategy_id=STRATEGY_A, account_id=ACCOUNT_A)
    result = engine.execute(intent)
    prm1.commit_reservation(decision.reservation_id, execution_result=result)

    recon_store = SqliteReconciliationStore(db_path=str(tmp_path / "recon.db"))
    service = ReconciliationService(recon_store, idem_store, audit_trail, worker_id="w1", portfolio_risk_manager=prm1)
    [rid] = service.scan_and_register()
    broker.order_book = []  # searched, genuinely not found
    outcome = service.reconcile_one(rid, broker)
    assert outcome.status == ReconciliationStatus.RECONCILED_NOT_FOUND.value

    snapshot = prm1.get_snapshot(strategy_id=STRATEGY_A, account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 0.0  # released
    del prm1

    # Restart -- released state must be durable, and Section 23's
    # fail-closed policy holds: no automatic new attempt of any kind.
    prm2 = _risk_manager(tmp_path, idempotency_store=idem_store)
    snapshot = prm2.get_snapshot(strategy_id=STRATEGY_A, account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 0.0
    assert snapshot.strategy_open_orders == 0
    assert broker.place_order_calls == 1  # never a second attempt


def test_ambiguous_then_reconciliation_unresolved_then_restart_stays_held(tmp_path):
    prm1 = _risk_manager(tmp_path)
    audit_trail, idem_store, broker, engine = _stack(tmp_path, portfolio_risk_manager=prm1)
    broker.raise_on_place = True
    intent = _intent(idempotency_key="e2e-unresolved-1")

    decision = prm1.evaluate_and_reserve(intent, strategy_id=STRATEGY_A, account_id=ACCOUNT_A)
    result = engine.execute(intent)
    prm1.commit_reservation(decision.reservation_id, execution_result=result)

    recon_store = SqliteReconciliationStore(db_path=str(tmp_path / "recon.db"))
    service = ReconciliationService(recon_store, idem_store, audit_trail, worker_id="w1", portfolio_risk_manager=prm1)
    [rid] = service.scan_and_register()

    import trading.common.reconciliation as recon_mod

    class _FailingView(recon_mod.ReadOnlyBrokerView):
        def get_order_book(self):
            raise ConnectionError("order book query failed")

        def get_open_orders(self):
            raise ConnectionError("open orders query failed")

    original = recon_mod.ReadOnlyBrokerView
    recon_mod.ReadOnlyBrokerView = _FailingView
    try:
        outcome = service.reconcile_one(rid, broker)
    finally:
        recon_mod.ReadOnlyBrokerView = original
    assert outcome.status == ReconciliationStatus.RECONCILIATION_UNKNOWN.value

    snapshot = prm1.get_snapshot(strategy_id=STRATEGY_A, account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 5000.0  # still held -- never guessed
    del prm1

    prm2 = _risk_manager(tmp_path, idempotency_store=idem_store)
    snapshot = prm2.get_snapshot(strategy_id=STRATEGY_A, account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 5000.0
    assert snapshot.strategy_open_orders == 1
    assert broker.place_order_calls == 1
