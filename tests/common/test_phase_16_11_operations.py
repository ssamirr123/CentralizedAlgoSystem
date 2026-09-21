"""Phase 16.11: OperationsSnapshot + OperationalAlertStore + WorkerRegistry/
WorkerCoordinator audit integration, end to end. Covers: worker lifecycle
audit events (no heartbeat spam), portfolio-risk audit integration,
execution-outcome alerts, failure-injection scenarios (worker/market-data/
risk/kill-switch/strategy failure), a three-worker operations scenario
proving cross-worker isolation of alerts, and structural safety."""
from __future__ import annotations

import datetime as dt
import inspect

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.kill_switch import CentralKillSwitch
from trading.common.market_data_gateway import FixedMarketDataSource
from trading.common.observability import AuditTrail
from trading.common.operational_alerts import OperationalAlertStore
from trading.common.operations_snapshot import build_operations_snapshot
from trading.common.order_intent import OrderIntent
from trading.common.portfolio_risk import PortfolioRiskLimits, PortfolioRiskManager
from trading.common.risk_manager import RiskManager
from trading.common.strategy import BaseStrategy
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_registry import StrategyRegistry
from trading.common.strategy_runtime import RuntimeState, StrategyRuntime
from trading.common.trading_account import ExecutionMode, TradingAccount
from trading.common.worker_coordinator import WorkerCoordinator
from trading.common.worker_protocol import OrderIntentSubmission
from trading.common.worker_registry import WorkerRegistry

STRATEGY_A = "StrategyA"
_SENTINEL = object()


class _OneShotStrategy(BaseStrategy):
    def __init__(self, strategy_id: str, account_id: str, idempotency_key: str = "k1", **kw) -> None:
        super().__init__(strategy_id, **kw)
        self._account_id = account_id
        self._idempotency_key = idempotency_key

    def _on_generate_order_intents(self):
        return []

    def make_intent(self, quantity: int = 1, symbol: str = "NIFTY", limit_price: float | None = 100.0, idempotency_key=_SENTINEL) -> OrderIntent:
        key = self._idempotency_key if idempotency_key is _SENTINEL else idempotency_key
        return OrderIntent(
            strategy_id=self.strategy_id, account_id=self._account_id, symbol=symbol, exchange="NFO",
            side=OrderSide.BUY, quantity=quantity,
            order_type=OrderType.LIMIT if limit_price is not None else OrderType.MARKET,
            limit_price=limit_price, idempotency_key=key,
        )


class _RequiresMarketDataStrategy(BaseStrategy):
    def __init__(self, strategy_id: str, instrument: str, **kw) -> None:
        super().__init__(strategy_id, **kw)
        self._instrument = instrument

    def required_instruments(self):
        return (self._instrument,)

    def _on_generate_order_intents(self):
        return []


class _AlwaysFailsStrategy(BaseStrategy):
    def _on_generate_order_intents(self):
        raise RuntimeError("simulated strategy decision-logic bug")


def _full_stack(*, extra_strategies=(), clock=None, audit_trail=None, operational_alerts=None, portfolio_risk_manager=None):
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id="ACC1", account_name="A", broker_id="paper", execution_mode=ExecutionMode.SHADOW),
        broker_client=PaperBroker(),
    )
    registry = StrategyRegistry()
    strategy = _OneShotStrategy(STRATEGY_A, "ACC1")
    registry.register(strategy)
    for s in extra_strategies:
        registry.register(s)
    assignment = StrategyAssignment(manager)
    assignment.assign(STRATEGY_A, "ACC1")
    registry.enable(STRATEGY_A)
    registry.start(STRATEGY_A)
    for s in extra_strategies:
        assignment.assign(s.strategy_id, "ACC1")
        registry.enable(s.strategy_id)
        registry.start(s.strategy_id)
    risk_manager = RiskManager(assignment)
    kill_switch = CentralKillSwitch()
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch,
        market_data_source=FixedMarketDataSource(),
    )
    workers = WorkerRegistry(clock=clock, audit_trail=audit_trail) if clock else WorkerRegistry(audit_trail=audit_trail)
    coordinator = WorkerCoordinator(
        worker_registry=workers, strategy_registry=registry, strategy_assignment=assignment, strategy_runtime=runtime,
        portfolio_risk_manager=portfolio_risk_manager, audit_trail=audit_trail, operational_alerts=operational_alerts,
    )
    return dict(
        manager=manager, registry=registry, assignment=assignment, runtime=runtime, workers=workers,
        coordinator=coordinator, strategy=strategy, kill_switch=kill_switch,
    )


def _submit(coordinator, workers, worker_id, session_id, strategy_id, intent, **overrides):
    kwargs = dict(worker_id=worker_id, session_id=session_id, strategy_id=strategy_id, evaluation_id="eval-1", intent=intent)
    kwargs.update(overrides)
    return coordinator.submit_order_intent(OrderIntentSubmission(**kwargs))


def _snapshot(stack, *, operational_alerts=None, portfolio_risk_manager=None, db_ready=True):
    return build_operations_snapshot(
        strategy_registry=stack["registry"], strategy_assignment=stack["assignment"],
        broker_manager=stack["manager"], kill_switch=stack["kill_switch"], strategy_runtime=stack["runtime"],
        worker_registry=stack["workers"], portfolio_risk_manager=portfolio_risk_manager,
        operational_alerts=operational_alerts, db_ready=db_ready,
    )


# --------------------------------------------------------------------------- #
# WorkerRegistry audit events -- transitions only, never heartbeat spam
# --------------------------------------------------------------------------- #
def test_register_worker_audits_registered_and_online():
    audit = AuditTrail()
    workers = WorkerRegistry(audit_trail=audit)
    workers.register_worker(worker_id="w1", name="A")
    events = [r.event_type for r in audit.records()]
    assert "WORKER_REGISTERED" in events
    assert "WORKER_ONLINE" in events


def test_normal_heartbeats_produce_zero_audit_events():
    audit = AuditTrail()
    workers = WorkerRegistry(audit_trail=audit)
    worker = workers.register_worker(worker_id="w1", name="A")
    before = len(audit.records())
    for _ in range(20):
        workers.record_heartbeat("w1", session_id=worker.session_id)
    assert len(audit.records()) == before  # zero new events -- Section 14


def test_heartbeat_timeout_audits_lost_and_offline_exactly_once():
    now = [dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)]
    audit = AuditTrail()
    workers = WorkerRegistry(clock=lambda: now[0], audit_trail=audit)
    workers.register_worker(worker_id="w1", name="A")
    now[0] += dt.timedelta(seconds=60)

    workers.list_workers()  # first lazy recompute -- transition happens here
    events_after_first = [r.event_type for r in audit.records()]
    assert events_after_first.count("WORKER_HEARTBEAT_LOST") == 1
    assert events_after_first.count("WORKER_OFFLINE") == 1

    workers.list_workers()  # second read -- must NOT re-audit (already OFFLINE)
    events_after_second = [r.event_type for r in audit.records()]
    assert events_after_second.count("WORKER_HEARTBEAT_LOST") == 1


def test_mark_offline_audits_worker_offline():
    audit = AuditTrail()
    workers = WorkerRegistry(audit_trail=audit)
    workers.register_worker(worker_id="w1", name="A")
    workers.mark_offline("w1")
    assert "WORKER_OFFLINE" in [r.event_type for r in audit.records()]


def test_duplicate_registration_while_online_audits_session_replacement_rejected():
    audit = AuditTrail()
    workers = WorkerRegistry(audit_trail=audit)
    workers.register_worker(worker_id="w1", name="A")
    try:
        workers.register_worker(worker_id="w1", name="A")
    except Exception:
        pass
    assert "WORKER_SESSION_REPLACEMENT_REJECTED" in [r.event_type for r in audit.records()]


def test_heartbeat_after_offline_recovery_audits_worker_online():
    now = [dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)]
    audit = AuditTrail()
    workers = WorkerRegistry(clock=lambda: now[0], audit_trail=audit)
    worker = workers.register_worker(worker_id="w1", name="A")
    now[0] += dt.timedelta(seconds=60)
    workers.list_workers()  # goes OFFLINE
    count_before = len([r for r in audit.records() if r.event_type == "WORKER_ONLINE"])

    workers.record_heartbeat("w1", session_id=worker.session_id)
    count_after = len([r for r in audit.records() if r.event_type == "WORKER_ONLINE"])
    assert count_after == count_before + 1


def test_assign_and_unassign_strategy_are_audited():
    audit = AuditTrail()
    workers = WorkerRegistry(audit_trail=audit)
    workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy("StrategyA", "w1")
    workers.unassign_strategy("StrategyA")
    events = [r.event_type for r in audit.records()]
    assert "STRATEGY_WORKER_ASSIGNED" in events
    assert "STRATEGY_WORKER_UNASSIGNED" in events


# --------------------------------------------------------------------------- #
# Portfolio-risk audit integration (Section 13)
# --------------------------------------------------------------------------- #
def test_portfolio_risk_decision_is_audited_on_allow_and_reject():
    audit = AuditTrail()
    prm_allow = PortfolioRiskManager()
    stack = _full_stack(audit_trail=audit, portfolio_risk_manager=prm_allow)
    worker = stack["workers"].register_worker(worker_id="w1", name="A")
    stack["workers"].assign_strategy(STRATEGY_A, "w1")

    _submit(stack["coordinator"], stack["workers"], "w1", worker.session_id, STRATEGY_A, stack["strategy"].make_intent(idempotency_key="a"))
    decisions = [r for r in audit.records() if r.event_type == "PORTFOLIO_RISK_DECISION"]
    assert len(decisions) == 1
    assert decisions[0].detail["allowed"] is True

    prm_block = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=1.0))
    stack2 = _full_stack(audit_trail=audit, portfolio_risk_manager=prm_block)
    worker2 = stack2["workers"].register_worker(worker_id="w2", name="B")
    stack2["workers"].assign_strategy(STRATEGY_A, "w2")
    _submit(stack2["coordinator"], stack2["workers"], "w2", worker2.session_id, STRATEGY_A, stack2["strategy"].make_intent(quantity=10, limit_price=100.0, idempotency_key="b"))
    decisions2 = [r for r in audit.records() if r.event_type == "PORTFOLIO_RISK_DECISION" and not r.detail["allowed"]]
    assert len(decisions2) == 1
    assert decisions2[0].detail["reason_code"] == "PORTFOLIO_EXPOSURE_LIMIT"


# --------------------------------------------------------------------------- #
# Execution-outcome alerts
# --------------------------------------------------------------------------- #
def test_portfolio_risk_block_raises_and_resolves_alert():
    alerts = OperationalAlertStore()
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=1.0))
    stack = _full_stack(operational_alerts=alerts, portfolio_risk_manager=prm)
    worker = stack["workers"].register_worker(worker_id="w1", name="A")
    stack["workers"].assign_strategy(STRATEGY_A, "w1")

    _submit(stack["coordinator"], stack["workers"], "w1", worker.session_id, STRATEGY_A, stack["strategy"].make_intent(quantity=10, limit_price=100.0, idempotency_key="a"))
    codes = {a.code for a in alerts.active_alerts()}
    assert "PORTFOLIO_RISK_BLOCKED" in codes

    # Raise the limit and retry -- allowed this time, alert must resolve.
    prm.set_portfolio_limits(PortfolioRiskLimits(max_portfolio_exposure=1_000_000.0))
    _submit(stack["coordinator"], stack["workers"], "w1", worker.session_id, STRATEGY_A, stack["strategy"].make_intent(quantity=10, limit_price=100.0, idempotency_key="b"))
    codes2 = {a.code for a in alerts.active_alerts()}
    assert "PORTFOLIO_RISK_BLOCKED" not in codes2


def test_existing_risk_manager_rejection_raises_execution_rejected_alert():
    from trading.common.risk_manager import RiskLimits

    alerts = OperationalAlertStore()
    stack = _full_stack(operational_alerts=alerts)
    # Re-wire with a tight RiskLimits so the underlying RiskManager rejects.
    stack["runtime"] = StrategyRuntime(
        strategy_registry=stack["registry"], strategy_assignment=stack["assignment"], broker_manager=stack["manager"],
        risk_manager=RiskManager(stack["assignment"], limits=RiskLimits(max_order_quantity=1)),
        kill_switch=stack["kill_switch"],
    )
    coordinator = WorkerCoordinator(
        worker_registry=stack["workers"], strategy_registry=stack["registry"], strategy_assignment=stack["assignment"],
        strategy_runtime=stack["runtime"], operational_alerts=alerts,
    )
    worker = stack["workers"].register_worker(worker_id="w1", name="A")
    stack["workers"].assign_strategy(STRATEGY_A, "w1")

    result = _submit(coordinator, stack["workers"], "w1", worker.session_id, STRATEGY_A, stack["strategy"].make_intent(quantity=10, idempotency_key="a"))
    assert result.accepted is False
    codes = {a.code for a in alerts.active_alerts()}
    assert "EXECUTION_REJECTED" in codes

    result2 = _submit(coordinator, stack["workers"], "w1", worker.session_id, STRATEGY_A, stack["strategy"].make_intent(quantity=1, idempotency_key="b"))
    assert result2.accepted is True
    codes2 = {a.code for a in alerts.active_alerts()}
    assert "EXECUTION_REJECTED" not in codes2


# --------------------------------------------------------------------------- #
# Failure injection (Section 37)
# --------------------------------------------------------------------------- #
def test_worker_failure_scenario():
    now = [dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)]
    alerts = OperationalAlertStore()
    stack = _full_stack(clock=lambda: now[0])
    stack["workers"].register_worker(worker_id="w1", name="A")
    now[0] += dt.timedelta(seconds=60)

    snapshot = _snapshot(stack, operational_alerts=alerts)
    worker_view = {w.worker_id: w for w in snapshot.workers}["w1"]
    assert worker_view.status == "OFFLINE"
    assert any(a.code == "WORKER_HEARTBEAT_LOST" for a in snapshot.active_alerts)


def test_market_data_failure_scenario():
    strategy = _RequiresMarketDataStrategy("NeedsData", "SOME_INSTRUMENT")
    alerts = OperationalAlertStore()
    stack = _full_stack(extra_strategies=(strategy,))
    # FixedMarketDataSource with no quote set for SOME_INSTRUMENT -> NO_DATA.
    stack["runtime"].run_once("NeedsData")

    snapshot = _snapshot(stack, operational_alerts=alerts)
    view = {s.strategy_id: s for s in snapshot.strategies}["NeedsData"]
    assert view.market_data_status == "NO_DATA"
    assert any(a.code == "MARKET_DATA_MISSING" and a.source_id == "NeedsData" for a in snapshot.active_alerts)


def test_kill_switch_failure_scenario_blocks_all_workers_and_shows_engaged():
    alerts = OperationalAlertStore()
    stack = _full_stack()
    worker = stack["workers"].register_worker(worker_id="w1", name="A")
    stack["workers"].assign_strategy(STRATEGY_A, "w1")
    stack["kill_switch"].engage(reason="halt", by="operator")

    result = _submit(stack["coordinator"], stack["workers"], "w1", worker.session_id, STRATEGY_A, stack["strategy"].make_intent(idempotency_key="a"))
    assert result.accepted is False

    snapshot = _snapshot(stack, operational_alerts=alerts)
    assert snapshot.safety.kill_switch_engaged is True
    assert any(a.code == "KILL_SWITCH_ENGAGED" for a in snapshot.active_alerts)


def test_strategy_failure_scenario():
    failing = _AlwaysFailsStrategy("AlwaysFails")
    alerts = OperationalAlertStore()
    stack = _full_stack(extra_strategies=(failing,))
    stack["runtime"].run_once("AlwaysFails")
    assert stack["runtime"].get_status("AlwaysFails").state == RuntimeState.FAILED

    snapshot = _snapshot(stack, operational_alerts=alerts)
    view = {s.strategy_id: s for s in snapshot.strategies}["AlwaysFails"]
    assert view.runtime_state == "FAILED"
    assert any(a.code == "STRATEGY_RUNTIME_FAILED" and a.source_id == "AlwaysFails" for a in snapshot.active_alerts)


# --------------------------------------------------------------------------- #
# Three-worker operations scenario (Section 38)
# --------------------------------------------------------------------------- #
def test_three_worker_operations_scenario_isolation_and_reconnect():
    now = [dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)]
    audit = AuditTrail()
    alerts = OperationalAlertStore(audit_trail=audit)

    manager = BrokerManager()
    for account_id in ("ACC_A", "ACC_B", "ACC_C"):
        manager.register_account(
            TradingAccount(account_id=account_id, account_name=account_id, broker_id="paper", execution_mode=ExecutionMode.SHADOW),
            broker_client=PaperBroker(),
        )
    registry = StrategyRegistry()
    strat_a = _OneShotStrategy("StratA", "ACC_A", idempotency_key="a-1")
    strat_b = _OneShotStrategy("StratB", "ACC_B", idempotency_key="b-1")
    strat_c = _OneShotStrategy("StratC", "ACC_C", idempotency_key="c-1")
    for s in (strat_a, strat_b, strat_c):
        registry.register(s)
    assignment = StrategyAssignment(manager)
    for s, acc in ((strat_a, "ACC_A"), (strat_b, "ACC_B"), (strat_c, "ACC_C")):
        assignment.assign(s.strategy_id, acc)
        registry.enable(s.strategy_id)
        registry.start(s.strategy_id)
    kill_switch = CentralKillSwitch()
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=RiskManager(assignment), kill_switch=kill_switch,
    )
    workers = WorkerRegistry(clock=lambda: now[0], audit_trail=audit)
    coordinator = WorkerCoordinator(
        worker_registry=workers, strategy_registry=registry, strategy_assignment=assignment, strategy_runtime=runtime,
        audit_trail=audit, operational_alerts=alerts,
    )
    w_a = workers.register_worker(worker_id="worker-a", name="Worker A")
    w_b = workers.register_worker(worker_id="worker-b", name="Worker B")
    w_c = workers.register_worker(worker_id="worker-c", name="Worker C")
    workers.assign_strategy("StratA", "worker-a")
    workers.assign_strategy("StratB", "worker-b")
    workers.assign_strategy("StratC", "worker-c")

    def stack_view():
        return dict(manager=manager, registry=registry, assignment=assignment, runtime=runtime, workers=workers, kill_switch=kill_switch)

    snap0 = _snapshot(stack_view(), operational_alerts=alerts)
    assert {w.status for w in snap0.workers} == {"ONLINE"}
    assert snap0.safety.execution_mode_banner == "SHADOW"
    assert snap0.active_alerts == ()

    # Workers A and C keep heartbeating; Worker B does not -- only B's
    # heartbeat should go stale and exceed the timeout.
    now[0] += dt.timedelta(seconds=25)
    workers.record_heartbeat("worker-a", session_id=w_a.session_id)
    workers.record_heartbeat("worker-c", session_id=w_c.session_id)
    now[0] += dt.timedelta(seconds=10)
    snap1 = _snapshot(stack_view(), operational_alerts=alerts)
    statuses = {w.worker_id: w.status for w in snap1.workers}
    assert statuses["worker-a"] == "ONLINE"
    assert statuses["worker-b"] == "OFFLINE"
    assert statuses["worker-c"] == "ONLINE"
    assert any(a.code == "WORKER_OFFLINE" and a.source_id == "worker-b" for a in snap1.active_alerts)
    # Other workers' strategies remain fully submittable -- unaffected.
    result_a = _submit(coordinator, workers, "worker-a", w_a.session_id, "StratA", strat_a.make_intent())
    assert result_a.accepted is True

    # Worker B cannot submit on its old (now-stale) session.
    stale_result = _submit(coordinator, workers, "worker-b", w_b.session_id, "StratB", strat_b.make_intent(idempotency_key="stale-attempt"))
    assert stale_result.accepted is False

    # Worker B reconnects (Phase 16.9 restart semantics): new session_id,
    # assignment preserved, and the reconnect itself makes no change
    # whatsoever to StrategyB's own lifecycle status (it was already
    # SHADOW from this test's own setup, deliberately independent of any
    # worker's online/offline state) -- proving the reconnect never
    # touches strategy lifecycle at all, let alone auto-starts anything.
    status_before_reconnect = registry.get("StratB").get_status()
    w_b_new = workers.register_worker(worker_id="worker-b", name="Worker B")
    assert w_b_new.session_id != w_b.session_id
    assert "StratB" in w_b_new.assigned_strategy_ids  # assignment preserved
    assert registry.get("StratB").get_status() == status_before_reconnect

    snap2 = _snapshot(stack_view(), operational_alerts=alerts)
    assert {w.worker_id: w.status for w in snap2.workers}["worker-b"] == "ONLINE"
    assert not any(a.code == "WORKER_OFFLINE" and a.source_id == "worker-b" for a in snap2.active_alerts)

    # The OLD session is still rejected even after reconnect (no replay).
    old_session_result = _submit(coordinator, workers, "worker-b", w_b.session_id, "StratB", strat_b.make_intent(idempotency_key="replay-attempt"))
    assert old_session_result.accepted is False
    assert "session_id" in old_session_result.reason

    # No real broker mutation anywhere.
    for acc in ("ACC_A", "ACC_B", "ACC_C"):
        assert manager.get_broker(acc).__class__.__name__ == "PaperBroker"


# --------------------------------------------------------------------------- #
# Structural safety (Section 42)
# --------------------------------------------------------------------------- #
def test_operations_modules_never_import_a_broker_adapter_or_sdk():
    import trading.common.operations_snapshot as ops_module
    import trading.common.operational_alerts as alerts_module

    for module in (ops_module, alerts_module):
        source = inspect.getsource(module)
        for forbidden in ("angelone", "AngelOne", "dhan", "Dhan", "icici_breeze", "ICICIBreeze", "smart_api", "SmartApi"):
            assert forbidden not in source


def test_operations_modules_never_import_live_authorization():
    import trading.common.operations_snapshot as ops_module
    import trading.common.operational_alerts as alerts_module

    for module in (ops_module, alerts_module):
        assert not hasattr(module, "LiveAuthorization")
        source = inspect.getsource(module)
        assert "live_authorization" not in source.lower()


def test_build_operations_snapshot_never_mutates_risk_limits_or_starts_a_strategy():
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=100.0))
    stack = _full_stack(portfolio_risk_manager=prm)
    before_status = stack["registry"].get(STRATEGY_A).get_status()
    before_limits = prm.get_portfolio_limits()

    for _ in range(5):
        _snapshot(stack, portfolio_risk_manager=prm)

    assert stack["registry"].get(STRATEGY_A).get_status() == before_status
    assert prm.get_portfolio_limits() == before_limits


def test_operations_snapshot_module_never_calls_engine_execute_directly():
    import trading.common.operations_snapshot as ops_module

    source = inspect.getsource(ops_module)
    assert ".execute(" not in source
    assert "place_order" not in source
    assert "modify_order" not in source
    assert "cancel_order" not in source
