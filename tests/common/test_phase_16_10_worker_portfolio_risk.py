"""Phase 16.10: PortfolioRiskManager wired into WorkerCoordinator -- proves
the additive gate ordering (OrderIntent -> WorkerCoordinator's own Phase
16.9 checks -> PortfolioRiskManager -> StrategyRuntime.execute_worker_intent
-> existing RiskManager, unchanged), that the existing RiskManager/kill
switch/idempotency remain fully authoritative, that a downstream rejection
releases the portfolio-risk reservation, that an idempotent retry never
double-reserves, and a three-real-strategy multi-worker end-to-end scenario
with both an accepted and a portfolio-risk-rejected submission."""
from __future__ import annotations

import datetime as dt
import inspect

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.kill_switch import CentralKillSwitch
from trading.common.order_intent import OrderIntent
from trading.common.portfolio_risk import PortfolioRiskLimits, PortfolioRiskManager
from trading.common.risk_manager import RiskLimits, RiskManager
from trading.common.strategies.combined_vwap_nifty import CombinedVwapNiftyStrategy
from trading.common.strategies.double_straddle import DoubleStraddleStrategy
from trading.common.strategies.vwap_algo_nifty_hedge import VwapAlgoNiftyHedgeStrategy
from trading.common.strategy import BaseStrategy
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_registry import StrategyRegistry
from trading.common.strategy_runtime import StrategyRuntime
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


def _stack(account_id="ACC1", risk_limits=None, portfolio_risk_manager=None):
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id=account_id, account_name="A", broker_id="paper", execution_mode=ExecutionMode.SHADOW),
        broker_client=PaperBroker(),
    )
    registry = StrategyRegistry()
    strategy = _OneShotStrategy(STRATEGY_A, account_id)
    registry.register(strategy)
    assignment = StrategyAssignment(manager)
    assignment.assign(STRATEGY_A, account_id)
    registry.enable(STRATEGY_A)
    registry.start(STRATEGY_A)
    risk_manager = RiskManager(assignment, limits=risk_limits)
    kill_switch = CentralKillSwitch()
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch,
    )
    workers = WorkerRegistry()
    coordinator = WorkerCoordinator(
        worker_registry=workers, strategy_registry=registry, strategy_assignment=assignment, strategy_runtime=runtime,
        portfolio_risk_manager=portfolio_risk_manager,
    )
    return manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch


def _submit(coordinator, workers, worker_id, session_id, strategy_id, intent, **overrides):
    kwargs = dict(worker_id=worker_id, session_id=session_id, strategy_id=strategy_id, evaluation_id="eval-1", intent=intent)
    kwargs.update(overrides)
    return coordinator.submit_order_intent(OrderIntentSubmission(**kwargs))


# --------------------------------------------------------------------------- #
# Gate ordering / additive integration
# --------------------------------------------------------------------------- #
def test_within_limits_is_accepted_and_updates_the_ledger():
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=10_000.0))
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack(portfolio_risk_manager=prm)
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")

    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(quantity=10, limit_price=100.0))
    assert result.accepted is True
    snapshot = prm.get_snapshot(strategy_id=STRATEGY_A, account_id="ACC1")
    assert snapshot.strategy_exposure == 1000.0


def test_portfolio_risk_rejects_before_reaching_the_execution_engine():
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=1.0))
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack(portfolio_risk_manager=prm)
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")

    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(quantity=10, limit_price=100.0))
    assert result.accepted is False
    assert "PORTFOLIO_EXPOSURE_LIMIT" in result.reason
    assert result.execution_result is None  # never reached execute_worker_intent


def test_existing_risk_manager_still_rejects_even_when_portfolio_risk_allows():
    # Existing RiskManager's own MAX_ORDER_QUANTITY must remain fully
    # authoritative -- portfolio risk allowing an order never overrides it.
    prm = PortfolioRiskManager()  # no portfolio limits configured -- always allows
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack(
        risk_limits=RiskLimits(max_order_quantity=1), portfolio_risk_manager=prm,
    )
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")

    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(quantity=10))
    assert result.accepted is False
    assert result.execution_result is not None
    assert "MAX_ORDER_QUANTITY" in result.execution_result.message

    # The portfolio-risk reservation must have been released -- the day's
    # order-count budget was NOT permanently consumed by a rejected order.
    snapshot = prm.get_snapshot(strategy_id=STRATEGY_A, account_id="ACC1")
    assert snapshot.strategy_exposure == 0.0


def test_kill_switch_blocks_worker_submissions_even_with_portfolio_risk_wired():
    prm = PortfolioRiskManager()
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack(portfolio_risk_manager=prm)
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    kill_switch.engage(reason="halt", by="operator")

    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(idempotency_key="k1"))
    assert result.accepted is False


# --------------------------------------------------------------------------- #
# Idempotency ordering (Section 23)
# --------------------------------------------------------------------------- #
def test_idempotent_retry_does_not_double_reserve_exposure():
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=10_000.0))
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack(portfolio_risk_manager=prm)
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")

    intent = strategy.make_intent(quantity=10, limit_price=100.0, idempotency_key="same-key")
    first = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, intent, submission_id="sub-1")
    second = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, intent, submission_id="sub-2")

    assert first.accepted is True
    assert second.accepted is True
    assert first.execution_result.order_id == second.execution_result.order_id  # replayed, not re-executed

    snapshot = prm.get_snapshot(strategy_id=STRATEGY_A, account_id="ACC1")
    assert snapshot.strategy_exposure == 1000.0  # NOT 2000 -- not double-counted


def test_cross_strategy_idempotency_key_reuse_does_not_corrupt_portfolio_state():
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=10_000.0))
    manager = BrokerManager()
    for account_id in ("ACC1", "ACC2"):
        manager.register_account(
            TradingAccount(account_id=account_id, account_name=account_id, broker_id="paper", execution_mode=ExecutionMode.SHADOW),
            broker_client=PaperBroker(),
        )
    registry = StrategyRegistry()
    s1 = _OneShotStrategy("S1", "ACC1")
    s2 = _OneShotStrategy("S2", "ACC2")
    registry.register(s1)
    registry.register(s2)
    assignment = StrategyAssignment(manager)
    assignment.assign("S1", "ACC1")
    assignment.assign("S2", "ACC2")
    for sid in ("S1", "S2"):
        registry.enable(sid)
        registry.start(sid)
    risk_manager = RiskManager(assignment)
    kill_switch = CentralKillSwitch()
    runtime = StrategyRuntime(strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager, risk_manager=risk_manager, kill_switch=kill_switch)
    workers = WorkerRegistry()
    coordinator = WorkerCoordinator(worker_registry=workers, strategy_registry=registry, strategy_assignment=assignment, strategy_runtime=runtime, portfolio_risk_manager=prm)

    w1 = workers.register_worker(worker_id="w1", name="A")
    w2 = workers.register_worker(worker_id="w2", name="B")
    workers.assign_strategy("S1", "w1")
    workers.assign_strategy("S2", "w2")

    first = _submit(coordinator, workers, "w1", w1.session_id, "S1", s1.make_intent(quantity=10, limit_price=100.0, idempotency_key="shared-key"))
    second = _submit(coordinator, workers, "w2", w2.session_id, "S2", s2.make_intent(quantity=10, limit_price=100.0, idempotency_key="shared-key"))

    assert first.accepted is True
    assert second.accepted is False  # IdempotencyKeyReuseError -- different intent, same key

    snap1 = prm.get_snapshot(strategy_id="S1", account_id="ACC1")
    snap2 = prm.get_snapshot(strategy_id="S2", account_id="ACC2")
    assert snap1.strategy_exposure == 1000.0
    assert snap2.strategy_exposure == 0.0  # S2's rejected attempt reserved nothing permanently


# --------------------------------------------------------------------------- #
# Worker isolation (Section 16/27) -- structural
# --------------------------------------------------------------------------- #
def test_worker_protocol_has_no_message_to_set_portfolio_limits():
    import trading.common.worker_protocol as protocol_module

    for name in ("WorkerRegistration", "WorkerHeartbeat", "StrategyStartCommand", "StrategyStopCommand",
                 "StrategyEvaluateCommand", "OrderIntentSubmission", "OrderIntentResult"):
        cls = getattr(protocol_module, name)
        field_names = {f for f in getattr(cls, "__dataclass_fields__", {})}
        for forbidden in ("limit", "portfolio_risk", "risk_limit"):
            assert not any(forbidden in f.lower() for f in field_names), f"{name} must not carry a risk-limit field"


def test_worker_coordinator_never_calls_portfolio_limit_setters():
    import trading.common.worker_coordinator as coordinator_module

    source = inspect.getsource(coordinator_module)
    for forbidden in ("set_strategy_limits(", "set_account_limits(", "set_portfolio_limits("):
        assert forbidden not in source


def test_worker_supplied_account_id_is_never_used_for_portfolio_risk_routing():
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=10_000.0))
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack(portfolio_risk_manager=prm)
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")

    # The intent claims a bogus account_id; the centrally-assigned "ACC1"
    # must be what portfolio risk reserves against, never the intent's own.
    bogus_intent = OrderIntent(
        strategy_id=STRATEGY_A, account_id="SOMEONE_ELSES_ACCOUNT", symbol="NIFTY", exchange="NFO",
        side=OrderSide.BUY, quantity=10, order_type=OrderType.LIMIT, limit_price=100.0, idempotency_key="k1",
    )
    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, bogus_intent)
    assert result.accepted is True

    real_account_snapshot = prm.get_snapshot(strategy_id=STRATEGY_A, account_id="ACC1")
    bogus_account_snapshot = prm.get_snapshot(strategy_id=STRATEGY_A, account_id="SOMEONE_ELSES_ACCOUNT")
    assert real_account_snapshot.account_exposure == 1000.0
    assert bogus_account_snapshot.account_exposure == 0.0


# --------------------------------------------------------------------------- #
# Structural safety -- no broker/credential/LiveAuthorization anywhere
# --------------------------------------------------------------------------- #
def test_portfolio_risk_module_never_imports_a_broker_adapter_or_sdk():
    import trading.common.portfolio_risk as portfolio_risk_module

    source = inspect.getsource(portfolio_risk_module)
    for forbidden in ("angelone", "AngelOne", "dhan", "Dhan", "icici_breeze", "ICICIBreeze", "smart_api", "SmartApi"):
        assert forbidden not in source


def test_portfolio_risk_module_never_imports_live_authorization():
    import trading.common.portfolio_risk as portfolio_risk_module

    assert "live_authorization" not in portfolio_risk_module.__dict__
    assert not hasattr(portfolio_risk_module, "LiveAuthorization")
    source = inspect.getsource(portfolio_risk_module)
    assert "import" not in "\n".join(
        line for line in source.splitlines() if "live_authorization" in line.lower() or "LiveAuthorization" in line
    )


# --------------------------------------------------------------------------- #
# Three-real-strategy multi-worker end-to-end (Section 28)
# --------------------------------------------------------------------------- #
def test_three_workers_end_to_end_with_one_accepted_and_one_portfolio_risk_rejection():
    manager = BrokerManager()
    for account_id in ("ACC_CVN", "ACC_DS", "ACC_VH"):
        manager.register_account(
            TradingAccount(account_id=account_id, account_name=account_id, broker_id="paper", execution_mode=ExecutionMode.SHADOW),
            broker_client=PaperBroker(),
        )
    registry = StrategyRegistry()
    cvn = CombinedVwapNiftyStrategy(ce_instrument="CVN_CE", pe_instrument="CVN_PE", account_id="ACC_CVN")
    ds = DoubleStraddleStrategy(
        hedge_ce_instrument="DS_HCE", hedge_pe_instrument="DS_HPE",
        straddle_ce_instrument="DS_SCE", straddle_pe_instrument="DS_SPE", account_id="ACC_DS",
        clock=lambda: dt.datetime(2026, 9, 19, 10, 20, 0),
    )
    vh = VwapAlgoNiftyHedgeStrategy(
        option_instrument="VH_OPT", hedge_instrument="VH_HEDGE", account_id="ACC_VH",
        clock=lambda: dt.datetime(2026, 9, 19, 9, 30, 0),
    )
    for s in (cvn, ds, vh):
        registry.register(s)
    assignment = StrategyAssignment(manager)
    assignment.assign("CombinedVwapNifty", "ACC_CVN")
    assignment.assign("DoubleStraddelAlgo", "ACC_DS")
    assignment.assign("Vwap_Algo_Nifty_hedge", "ACC_VH")
    for sid in ("CombinedVwapNifty", "DoubleStraddelAlgo", "Vwap_Algo_Nifty_hedge"):
        registry.enable(sid)
        registry.start(sid)

    risk_manager = RiskManager(assignment)
    kill_switch = CentralKillSwitch()
    runtime = StrategyRuntime(strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager, risk_manager=risk_manager, kill_switch=kill_switch)

    # Portfolio-wide order-count limit of 2 -- deliberately independent of
    # price/exposure data so this test doesn't depend on the real
    # strategies' own MARKET-order (no limit_price) intents.
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_orders_per_day=2))

    workers = WorkerRegistry()
    w_cvn = workers.register_worker(worker_id="worker-cvn", name="CombinedVWAP worker")
    w_ds = workers.register_worker(worker_id="worker-ds", name="DoubleStraddle worker")
    w_vh = workers.register_worker(worker_id="worker-vh", name="VWAPHedge worker")
    workers.assign_strategy("CombinedVwapNifty", "worker-cvn")
    workers.assign_strategy("DoubleStraddelAlgo", "worker-ds")
    workers.assign_strategy("Vwap_Algo_Nifty_hedge", "worker-vh")
    coordinator = WorkerCoordinator(
        worker_registry=workers, strategy_registry=registry, strategy_assignment=assignment, strategy_runtime=runtime,
        portfolio_risk_manager=prm,
    )

    from trading.market_data.schemas import OptionQuote

    def q(symbol, ltp):
        return OptionQuote(symbol=symbol, underlying="NIFTY", expiry=dt.date(2026, 9, 25), strike=24950.0, option_type="CE", ltp=ltp)

    ds_intents = ds.generate_order_intents({k: q(k, 100.0) for k in ("DS_HCE", "DS_HPE", "DS_SCE", "DS_SPE")})
    vh_intents = vh.generate_order_intents({k: q(k, 100.0) for k in ("VH_OPT", "VH_HEDGE")})
    assert len(ds_intents) == 2
    assert len(vh_intents) == 1

    result_1 = _submit(coordinator, workers, "worker-ds", w_ds.session_id, "DoubleStraddelAlgo", ds_intents[0])
    result_2 = _submit(coordinator, workers, "worker-vh", w_vh.session_id, "Vwap_Algo_Nifty_hedge", vh_intents[0])
    # Portfolio order-count budget (2) is now exhausted; a third,
    # otherwise-legitimate submission from its own correctly-owning worker
    # must be rejected by PortfolioRiskManager, not by any Phase 16.9 check.
    result_3 = _submit(coordinator, workers, "worker-ds", w_ds.session_id, "DoubleStraddelAlgo", ds_intents[1])

    assert result_1.accepted is True
    assert result_2.accepted is True
    assert result_3.accepted is False
    assert "ORDER_COUNT_LIMIT" in result_3.reason
    assert result_3.execution_result is None  # rejected before reaching the execution engine at all

    # No real broker mutation anywhere -- both accounts' brokers are PaperBroker.
    assert manager.get_broker("ACC_DS").__class__.__name__ == "PaperBroker"
    assert manager.get_broker("ACC_VH").__class__.__name__ == "PaperBroker"


def test_real_broker_place_order_call_count_is_zero_through_portfolio_risk():
    """Explicit REAL BROKER SAFETY test using a recording spy (same pattern
    as tests/common/test_strategy_runtime.py's own): proves
    place_order()/modify_order()/cancel_order() are never invoked on the
    resolved simulation broker even when PortfolioRiskManager sits in
    front of the existing pipeline -- ExecutionConfig(dry_run=True) still
    short-circuits before any of them, exactly as before this phase."""

    class _RecordingBroker(PaperBroker):
        def __init__(self):
            super().__init__()
            self.place_order_calls = 0
            self.modify_order_calls = 0
            self.cancel_order_calls = 0

        def place_order(self, *a, **kw):
            self.place_order_calls += 1
            return super().place_order(*a, **kw)

        def cancel_order(self, *a, **kw):
            self.cancel_order_calls += 1
            return super().cancel_order(*a, **kw)

    recording = _RecordingBroker()
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id="ACC1", account_name="A", broker_id="paper", execution_mode=ExecutionMode.SHADOW),
        broker_client=recording,
    )
    registry = StrategyRegistry()
    strategy = _OneShotStrategy(STRATEGY_A, "ACC1")
    registry.register(strategy)
    assignment = StrategyAssignment(manager)
    assignment.assign(STRATEGY_A, "ACC1")
    registry.enable(STRATEGY_A)
    registry.start(STRATEGY_A)
    risk_manager = RiskManager(assignment)
    kill_switch = CentralKillSwitch()
    runtime = StrategyRuntime(strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager, risk_manager=risk_manager, kill_switch=kill_switch)
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=10_000.0))
    workers = WorkerRegistry()
    coordinator = WorkerCoordinator(worker_registry=workers, strategy_registry=registry, strategy_assignment=assignment, strategy_runtime=runtime, portfolio_risk_manager=prm)
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")

    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(quantity=10, limit_price=100.0))

    assert result.accepted is True
    assert recording.place_order_calls == 0
    assert recording.modify_order_calls == 0
    assert recording.cancel_order_calls == 0


def test_kill_switch_blocks_all_three_workers_with_portfolio_risk_wired():
    prm = PortfolioRiskManager()
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack(portfolio_risk_manager=prm)
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    kill_switch.engage(reason="halt", by="operator")

    results = [
        _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(idempotency_key=f"k{i}"))
        for i in range(3)
    ]
    assert all(r.accepted is False for r in results)
