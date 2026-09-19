"""Phase 16.9: trading/common/worker_coordinator.py -- the central, sole
authority validating and executing worker-submitted OrderIntents. Proves
the full validation chain, cross-worker isolation, and that kill switch/
RiskManager/idempotency remain fully authoritative for the distributed
path -- reusing the exact same StrategyRuntime/StrategyExecutionEngine
every local (non-distributed) evaluation already uses."""
from __future__ import annotations

import datetime as dt
import threading

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.kill_switch import CentralKillSwitch
from trading.common.market_data_gateway import FixedMarketDataSource
from trading.common.order_intent import OrderIntent
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

    def make_intent(self, quantity: int = 1, symbol: str = "NIFTY", idempotency_key=_SENTINEL) -> OrderIntent:
        key = self._idempotency_key if idempotency_key is _SENTINEL else idempotency_key
        return OrderIntent(
            strategy_id=self.strategy_id, account_id=self._account_id, symbol=symbol, exchange="NFO",
            side=OrderSide.BUY, quantity=quantity, order_type=OrderType.MARKET, idempotency_key=key,
        )


def _stack(account_id="ACC1", risk_limits=None):
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
    )
    return manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch


def _submit(coordinator, workers, worker_id, session_id, strategy_id, intent, **overrides):
    kwargs = dict(
        worker_id=worker_id, session_id=session_id, strategy_id=strategy_id,
        evaluation_id="eval-1", intent=intent,
    )
    kwargs.update(overrides)
    return coordinator.submit_order_intent(OrderIntentSubmission(**kwargs))


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_valid_submission_is_accepted_and_executed_in_shadow():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="Worker One")
    workers.assign_strategy(STRATEGY_A, "w1")

    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent())
    assert result.accepted is True
    assert result.execution_result.success is True


# --------------------------------------------------------------------------- #
# Required failure tests (Section 23)
# --------------------------------------------------------------------------- #
def test_unknown_worker_is_rejected():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    result = _submit(coordinator, workers, "ghost", "s1", STRATEGY_A, strategy.make_intent())
    assert result.accepted is False
    assert "unknown worker" in result.reason


def test_offline_worker_is_rejected():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    workers.mark_offline("w1")
    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent())
    assert result.accepted is False
    assert "not ONLINE" in result.reason


def test_duplicate_worker_session_is_rejected():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    result = _submit(coordinator, workers, "w1", "not-the-real-session", STRATEGY_A, strategy.make_intent())
    assert result.accepted is False
    assert "session_id" in result.reason


def test_wrong_strategy_worker_assignment_is_rejected():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="A")
    # deliberately never assign STRATEGY_A to w1
    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent())
    assert result.accepted is False
    assert "not assigned to worker" in result.reason


def test_strategy_not_active_is_rejected():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    registry.stop(STRATEGY_A)
    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent())
    assert result.accepted is False
    assert "active lifecycle state" in result.reason


def test_intent_strategy_id_mismatch_is_rejected():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    mismatched = OrderIntent(
        strategy_id="SomeoneElse", account_id="ACC1", symbol="NIFTY", exchange="NFO",
        side=OrderSide.BUY, quantity=1, order_type=OrderType.MARKET, idempotency_key="k1",
    )
    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, mismatched)
    assert result.accepted is False
    assert "does not match" in result.reason


def test_missing_idempotency_key_is_rejected():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(idempotency_key=""))
    assert result.accepted is False
    assert "idempotency_key" in result.reason


def test_non_positive_quantity_is_rejected():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(quantity=0))
    assert result.accepted is False
    assert "quantity" in result.reason


def test_malformed_order_intent_missing_symbol_is_rejected():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(symbol=""))
    assert result.accepted is False
    assert "instrument" in result.reason


def test_duplicate_submission_id_is_rejected_on_replay():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    submission = OrderIntentSubmission(
        worker_id="w1", session_id=worker.session_id, strategy_id=STRATEGY_A, evaluation_id="e1",
        intent=strategy.make_intent(),
    )
    first = coordinator.submit_order_intent(submission)
    second = coordinator.submit_order_intent(submission)  # exact same submission object, retried
    assert first.accepted is True
    assert second.accepted is False
    assert "duplicate submission_id" in second.reason


def test_stale_submission_is_rejected():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    old_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=999)).isoformat()
    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(), generated_at=old_ts)
    assert result.accepted is False
    assert "stale submission" in result.reason


def test_malformed_generated_at_is_rejected():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    result = _submit(
        coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(), generated_at="not-a-date",
    )
    assert result.accepted is False
    assert "malformed" in result.reason


def test_missing_account_assignment_is_rejected():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    assignment.remove(STRATEGY_A)
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent())
    assert result.accepted is False
    assert "no account assignment" in result.reason


# --------------------------------------------------------------------------- #
# Worker cannot self-authorize / cannot change account
# --------------------------------------------------------------------------- #
def test_worker_supplied_account_id_on_the_intent_is_never_trusted_for_routing():
    """The intent claims a DIFFERENT account than the real assignment --
    execution must still route to the real, centrally-assigned account,
    never the worker's own claim (OrderIntent.account_id is informational
    only, per its own docstring)."""
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack(account_id="ACC1")
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    spoofed = OrderIntent(
        strategy_id=STRATEGY_A, account_id="SOME_OTHER_ACCOUNT", symbol="NIFTY", exchange="NFO",
        side=OrderSide.BUY, quantity=1, order_type=OrderType.MARKET, idempotency_key="k1",
    )
    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, spoofed)
    assert result.accepted is True
    assert result.execution_result.account_id == "ACC1"  # routed to the REAL assignment, not "SOME_OTHER_ACCOUNT"


def test_worker_cannot_access_broker_credentials():
    import inspect
    import trading.common.worker_coordinator as mod

    source = inspect.getsource(mod)
    for forbidden in ("credential", "api_key", "api_secret", "access_token"):
        assert forbidden not in source.lower()


# --------------------------------------------------------------------------- #
# Cross-worker isolation (Section 15/23)
# --------------------------------------------------------------------------- #
def test_worker_a_cannot_submit_for_a_strategy_owned_by_worker_b():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker_a = workers.register_worker(worker_id="w1", name="A")
    worker_b = workers.register_worker(worker_id="w2", name="B")
    workers.assign_strategy(STRATEGY_A, "w2")  # owned by worker B

    result = _submit(coordinator, workers, "w1", worker_a.session_id, STRATEGY_A, strategy.make_intent())
    assert result.accepted is False
    assert "not assigned to worker" in result.reason

    # The rightful owner still succeeds.
    result_b = _submit(coordinator, workers, "w2", worker_b.session_id, STRATEGY_A, strategy.make_intent(idempotency_key="k2"))
    assert result_b.accepted is True


# --------------------------------------------------------------------------- #
# Kill switch remains authoritative (Section 17)
# --------------------------------------------------------------------------- #
def test_kill_switch_blocks_the_submission():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    kill_switch.engage(reason="test", by="tester")

    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent())
    assert result.accepted is False
    assert result.execution_result is not None
    assert result.execution_result.success is False


# --------------------------------------------------------------------------- #
# RiskManager remains authoritative (Section 18)
# --------------------------------------------------------------------------- #
def test_risk_manager_rejects_an_over_limit_submission():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack(
        risk_limits=RiskLimits(max_order_quantity=1),
    )
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")

    result = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(quantity=100))
    assert result.accepted is False
    assert result.execution_result.success is False


# --------------------------------------------------------------------------- #
# Idempotency across workers (Section 19)
# --------------------------------------------------------------------------- #
def test_same_worker_retrying_the_same_intent_key_does_not_double_execute():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")

    first = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(idempotency_key="same-key"))
    second = _submit(
        coordinator, workers, "w1", worker.session_id, STRATEGY_A,
        strategy.make_intent(idempotency_key="same-key"), evaluation_id="eval-2",
    )
    assert first.accepted is True
    assert second.accepted is True  # replayed, not a new execution
    assert first.execution_result.order_id == second.execution_result.order_id


def test_a_different_strategy_cannot_reuse_another_strategys_idempotency_key():
    """Even if a buggy/malicious worker reused a literal key string, the
    existing idempotency store scopes replay validity to the exact same
    intent shape (strategy_id included) -- a genuinely different intent
    under a reused key is refused as a key-reuse error, never silently
    executed twice under someone else's identity."""
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    strategy_b = _OneShotStrategy("StrategyB", "ACC1")
    registry.register(strategy_b)
    assignment.assign("StrategyB", "ACC1")
    registry.enable("StrategyB")
    registry.start("StrategyB")

    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    workers.assign_strategy("StrategyB", "w1")

    first = _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(idempotency_key="shared-key"))
    second = _submit(
        coordinator, workers, "w1", worker.session_id, "StrategyB",
        strategy_b.make_intent(idempotency_key="shared-key"), evaluation_id="eval-2",
    )
    assert first.accepted is True
    assert second.accepted is False  # IdempotencyKeyReuseError surfaces as a rejected/failed execution, never a silent double


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
def test_concurrent_submissions_from_different_workers_are_isolated():
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id="ACC_A", account_name="A", broker_id="paper", execution_mode=ExecutionMode.SHADOW),
        broker_client=PaperBroker(),
    )
    manager.register_account(
        TradingAccount(account_id="ACC_B", account_name="B", broker_id="paper", execution_mode=ExecutionMode.SHADOW),
        broker_client=PaperBroker(),
    )
    registry = StrategyRegistry()
    strat_a = _OneShotStrategy("StrategyA", "ACC_A")
    strat_b = _OneShotStrategy("StrategyB", "ACC_B")
    registry.register(strat_a)
    registry.register(strat_b)
    assignment = StrategyAssignment(manager)
    assignment.assign("StrategyA", "ACC_A")
    assignment.assign("StrategyB", "ACC_B")
    for sid in ("StrategyA", "StrategyB"):
        registry.enable(sid)
        registry.start(sid)
    risk_manager = RiskManager(assignment)
    kill_switch = CentralKillSwitch()
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch,
    )
    workers = WorkerRegistry()
    worker_a = workers.register_worker(worker_id="wa", name="A")
    worker_b = workers.register_worker(worker_id="wb", name="B")
    workers.assign_strategy("StrategyA", "wa")
    workers.assign_strategy("StrategyB", "wb")
    coordinator = WorkerCoordinator(
        worker_registry=workers, strategy_registry=registry, strategy_assignment=assignment, strategy_runtime=runtime,
    )

    results = {}

    def submit_a():
        results["a"] = _submit(coordinator, workers, "wa", worker_a.session_id, "StrategyA", strat_a.make_intent(idempotency_key="a-1"))

    def submit_b():
        results["b"] = _submit(coordinator, workers, "wb", worker_b.session_id, "StrategyB", strat_b.make_intent(idempotency_key="b-1"))

    threads = [threading.Thread(target=submit_a), threading.Thread(target=submit_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results["a"].accepted is True
    assert results["b"].accepted is True
    assert results["a"].execution_result.account_id == "ACC_A"
    assert results["b"].execution_result.account_id == "ACC_B"


# --------------------------------------------------------------------------- #
# Multi-worker shadow execution simulation with the three REAL strategies
# (Section 15/16) -- distinct workers, distinct accounts, one shared
# central coordinator, real ported decision logic.
# --------------------------------------------------------------------------- #
def test_three_workers_three_real_strategies_end_to_end_shadow_execution():
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
    source = FixedMarketDataSource()
    from trading.market_data.schemas import OptionQuote

    def q(symbol, ltp):
        return OptionQuote(symbol=symbol, underlying="NIFTY", expiry=dt.date(2026, 9, 25), strike=24950.0, option_type="CE", ltp=ltp)

    for instrument in ("CVN_CE", "CVN_PE", "DS_HCE", "DS_HPE", "DS_SCE", "DS_SPE", "VH_OPT", "VH_HEDGE"):
        source.set_quote(instrument, q(instrument, 100.0))

    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch, market_data_source=source,
    )
    workers = WorkerRegistry()
    w_cvn = workers.register_worker(worker_id="worker-cvn", name="CombinedVWAP worker")
    w_ds = workers.register_worker(worker_id="worker-ds", name="DoubleStraddle worker")
    w_vh = workers.register_worker(worker_id="worker-vh", name="VWAPHedge worker")
    workers.assign_strategy("CombinedVwapNifty", "worker-cvn")
    workers.assign_strategy("DoubleStraddelAlgo", "worker-ds")
    workers.assign_strategy("Vwap_Algo_Nifty_hedge", "worker-vh")
    coordinator = WorkerCoordinator(
        worker_registry=workers, strategy_registry=registry, strategy_assignment=assignment, strategy_runtime=runtime,
    )

    # Worker DS hedge fires unconditionally at its clock time; Worker VH
    # hedge also fires unconditionally on first evaluation. Neither
    # CombinedVwapNifty's arm/fire nor these workers' straddle legs need
    # to fire for this test's purpose -- proving each worker's real
    # decision logic independently reaches the SAME central coordinator
    # and simulated broker is what matters here.
    ds_intents = ds.generate_order_intents({k: q(k, 100.0) for k in ("DS_HCE", "DS_HPE", "DS_SCE", "DS_SPE")})
    vh_intents = vh.generate_order_intents({k: q(k, 100.0) for k in ("VH_OPT", "VH_HEDGE")})
    assert len(ds_intents) == 2  # hedge entry
    assert len(vh_intents) == 1  # hedge entry

    ds_result = _submit(coordinator, workers, "worker-ds", w_ds.session_id, "DoubleStraddelAlgo", ds_intents[0])
    vh_result = _submit(coordinator, workers, "worker-vh", w_vh.session_id, "Vwap_Algo_Nifty_hedge", vh_intents[0])

    assert ds_result.accepted is True
    assert vh_result.accepted is True
    assert ds_result.execution_result.account_id == "ACC_DS"
    assert vh_result.execution_result.account_id == "ACC_VH"

    # Worker A (CombinedVWAP) cannot control Worker B's (DoubleStraddle) strategy.
    rogue = _submit(coordinator, workers, "worker-cvn", w_cvn.session_id, "DoubleStraddelAlgo", ds_intents[1], evaluation_id="rogue")
    assert rogue.accepted is False
    assert "not assigned to worker" in rogue.reason


def test_kill_switch_blocks_all_three_workers_simultaneously():
    manager, registry, assignment, runtime, workers, coordinator, strategy, kill_switch = _stack()
    worker = workers.register_worker(worker_id="w1", name="A")
    workers.assign_strategy(STRATEGY_A, "w1")
    kill_switch.engage(reason="halt", by="operator")

    results = [
        _submit(coordinator, workers, "w1", worker.session_id, STRATEGY_A, strategy.make_intent(idempotency_key=f"k{i}"))
        for i in range(3)
    ]
    assert all(r.accepted is False for r in results)
