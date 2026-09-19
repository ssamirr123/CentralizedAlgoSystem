"""Phase 16.5: trading/common/strategy_runtime.py -- proves the complete
Strategy -> OrderIntent -> StrategyExecutionEngine -> PAPER/SHADOW flow,
with a structural hard boundary against ever reaching a real broker."""
from __future__ import annotations

import threading

import pytest

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.kill_switch import CentralKillSwitch
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskManager
from trading.common.strategies.double_straddle import DoubleStraddleStrategy
from trading.common.strategy import BaseStrategy
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_registry import StrategyRegistry, UnknownStrategyError
from trading.common.strategy_runtime import RuntimeState, StrategyRuntime
from trading.common.trading_account import ExecutionMode, TradingAccount

STRATEGY_ID = "DoubleStraddelAlgo"


class _OneShotStrategy(BaseStrategy):
    """Test-only stub that generates exactly one real OrderIntent per
    active call -- the production Phase 10 strategy classes deliberately
    generate zero (see docs/phase-16-5-strategy-runtime-shadow-execution-report.md
    Section 3), so this is the vehicle used to prove intents that ARE
    generated actually flow all the way to a simulated result."""

    def __init__(self, strategy_id: str, account_id: str, *, idempotency_key: str = "runtime-test-1", **kw) -> None:
        super().__init__(strategy_id, **kw)
        self._account_id = account_id
        self._idempotency_key = idempotency_key
        self.calls = 0

    def _on_generate_order_intents(self):
        self.calls += 1
        return [
            OrderIntent(
                strategy_id=self.strategy_id, account_id=self._account_id, symbol="NIFTY24950CE",
                exchange="NFO", side=OrderSide.BUY, quantity=1, order_type=OrderType.MARKET,
                idempotency_key=self._idempotency_key,
            )
        ]


class _RaisingStrategy(BaseStrategy):
    def _on_generate_order_intents(self):
        raise RuntimeError("simulated strategy crash")


def _setup(*, execution_mode=ExecutionMode.SHADOW, broker_client=None):
    manager = BrokerManager()
    account = TradingAccount(
        account_id="ACC1", account_name="A1", broker_id="paper", execution_mode=execution_mode,
    )
    manager.register_account(account, broker_client=broker_client or PaperBroker())
    registry = StrategyRegistry()
    assignment = StrategyAssignment(manager)
    risk_manager = RiskManager(assignment)
    kill_switch = CentralKillSwitch()
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch,
    )
    return manager, registry, assignment, kill_switch, runtime


# --------------------------------------------------------------------------- #
# Basic run_once() behavior
# --------------------------------------------------------------------------- #
def test_unknown_strategy_raises():
    _, registry, _, _, runtime = _setup()
    with pytest.raises(UnknownStrategyError):
        runtime.run_once("NoSuchStrategy")


def test_inactive_strategy_is_a_pure_noop():
    manager, registry, assignment, kill_switch, runtime = _setup()
    strategy = DoubleStraddleStrategy()
    registry.register(strategy)  # DISABLED by default
    result = runtime.run_once(STRATEGY_ID)
    assert result.ticked is False
    assert result.intents_generated == 0
    assert result.executions == []


def test_production_strategy_generates_zero_intents_by_design():
    """The three registered Phase-10 strategy classes deliberately generate
    no real intents (Phase 10 scope) -- the runtime must tick cleanly and
    report zero executions, proving the pipe is wired without fabricating
    activity."""
    manager, registry, assignment, kill_switch, runtime = _setup()
    strategy = DoubleStraddleStrategy()
    registry.register(strategy)
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)
    result = runtime.run_once(STRATEGY_ID)
    assert result.ticked is True
    assert result.intents_generated == 0
    assert result.executions == []
    status = runtime.get_status(STRATEGY_ID)
    assert status.state == RuntimeState.HEALTHY


def test_one_shot_strategy_intent_reaches_a_simulated_execution():
    manager, registry, assignment, kill_switch, runtime = _setup()
    strategy = _OneShotStrategy(STRATEGY_ID, "ACC1")
    registry.register(strategy)
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)

    result = runtime.run_once(STRATEGY_ID)

    assert result.ticked is True
    assert result.intents_generated == 1
    assert len(result.executions) == 1
    assert result.executions[0].success is True
    assert result.error == ""
    status = runtime.get_status(STRATEGY_ID)
    assert status.state == RuntimeState.HEALTHY
    assert status.last_result_summary != ""


def test_generate_order_intents_crash_marks_error_never_running():
    manager, registry, assignment, kill_switch, runtime = _setup()
    strategy = _RaisingStrategy(STRATEGY_ID)
    registry.register(strategy)
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)

    result = runtime.run_once(STRATEGY_ID)

    assert result.ticked is True
    assert "simulated strategy crash" in result.error
    assert registry.get_status(STRATEGY_ID).value == "error"
    status = runtime.get_status(STRATEGY_ID)
    assert status.state == RuntimeState.FAILED
    assert status.last_error != ""


# --------------------------------------------------------------------------- #
# Hard shadow boundary
# --------------------------------------------------------------------------- #
def test_non_simulated_broker_is_refused_never_executed():
    """A broker that is NOT PaperBroker/ShadowBroker/ConnectedShadowBroker
    (here, a bare object whose place_order would raise if ever called)
    must be refused outright -- the runtime must never call execute() on
    it."""

    class _UnknownBrokerType:
        def place_order(self, *a, **kw):
            raise AssertionError("place_order must never be called on a non-simulated broker")

        def is_connected(self):
            return True

        def connect(self):
            return None

    manager, registry, assignment, kill_switch, runtime = _setup(broker_client=_UnknownBrokerType())
    strategy = _OneShotStrategy(STRATEGY_ID, "ACC1")
    registry.register(strategy)
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)

    result = runtime.run_once(STRATEGY_ID)

    assert "not a known-simulated broker" in result.error
    assert registry.get_status(STRATEGY_ID).value == "error"
    assert runtime.get_status(STRATEGY_ID).state == RuntimeState.FAILED


def test_intent_with_mismatched_strategy_id_is_refused():
    manager, registry, assignment, kill_switch, runtime = _setup()

    class _MismatchedStrategy(BaseStrategy):
        def _on_generate_order_intents(self):
            return [
                OrderIntent(
                    strategy_id="SomeoneElse", account_id="ACC1", symbol="NIFTY24950CE", exchange="NFO",
                    side=OrderSide.BUY, quantity=1, order_type=OrderType.MARKET, idempotency_key="mismatch-1",
                )
            ]

    strategy = _MismatchedStrategy(STRATEGY_ID)
    registry.register(strategy)
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)

    result = runtime.run_once(STRATEGY_ID)
    assert "does not match" in result.error
    assert registry.get_status(STRATEGY_ID).value == "error"


def test_dry_run_prevents_place_order_even_if_boundary_check_were_bypassed():
    """Layer-1 proof, independent of the broker-class check: a PaperBroker
    subclass whose place_order() would raise if called must never be
    reached, because ExecutionConfig(dry_run=True) short-circuits inside
    execute()'s own place_limit()/place_market_emergency() before ever
    calling active_broker.place_order()."""

    class _AssertingPaperBroker(PaperBroker):
        def place_order(self, *a, **kw):
            raise AssertionError("place_order must never be called -- dry_run must short-circuit first")

    manager, registry, assignment, kill_switch, runtime = _setup(broker_client=_AssertingPaperBroker())
    strategy = _OneShotStrategy(STRATEGY_ID, "ACC1")
    registry.register(strategy)
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)

    result = runtime.run_once(STRATEGY_ID)
    assert result.error == ""
    assert result.executions[0].success is True  # dry-run synthetic fill, no AssertionError raised


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def test_repeated_run_once_with_the_same_signal_does_not_double_execute():
    manager, registry, assignment, kill_switch, runtime = _setup()
    strategy = _OneShotStrategy(STRATEGY_ID, "ACC1", idempotency_key="same-signal")
    registry.register(strategy)
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)

    first = runtime.run_once(STRATEGY_ID)
    second = runtime.run_once(STRATEGY_ID)

    assert first.executions[0].success is True
    assert second.executions[0].success is True
    assert strategy.calls == 2  # the strategy IS re-evaluated each cycle...
    # ...but the underlying, pre-existing idempotency store recognizes the
    # replayed key and returns the exact SAME cached result (same
    # order_id) rather than performing a second, independent dry-run fill
    # (which would mint a new DRYRUN-<timestamp> order_id).
    assert second.executions[0].order_id == first.executions[0].order_id


# --------------------------------------------------------------------------- #
# Multi-account isolation
# --------------------------------------------------------------------------- #
def test_two_strategies_on_different_accounts_never_cross_execute():
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
    assignment = StrategyAssignment(manager)
    risk_manager = RiskManager(assignment)
    kill_switch = CentralKillSwitch()
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch,
    )

    strat_a = _OneShotStrategy("StrategyA", "ACC_A", idempotency_key="a-1")
    strat_b = _OneShotStrategy("StrategyB", "ACC_B", idempotency_key="b-1")
    registry.register(strat_a)
    registry.register(strat_b)
    assignment.assign("StrategyA", "ACC_A")
    assignment.assign("StrategyB", "ACC_B")
    registry.enable("StrategyA")
    registry.start("StrategyA")
    registry.enable("StrategyB")
    registry.start("StrategyB")

    result_a = runtime.run_once("StrategyA")
    result_b = runtime.run_once("StrategyB")

    assert result_a.executions[0].account_id == "ACC_A"
    assert result_b.executions[0].account_id == "ACC_B"
    assert manager.get_broker("ACC_A") is not manager.get_broker("ACC_B")


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
def test_concurrent_run_once_on_the_same_strategy_is_serialized_safely():
    manager, registry, assignment, kill_switch, runtime = _setup()
    strategy = _OneShotStrategy(STRATEGY_ID, "ACC1", idempotency_key="concurrent-1")
    registry.register(strategy)
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)

    results = []
    lock = threading.Lock()

    def worker():
        r = runtime.run_once(STRATEGY_ID)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 6
    assert all(r.error == "" for r in results)
    assert registry.get_status(STRATEGY_ID).value == "shadow"  # never corrupted


def test_concurrent_run_once_on_different_strategies_is_independent():
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
    assignment = StrategyAssignment(manager)
    risk_manager = RiskManager(assignment)
    kill_switch = CentralKillSwitch()
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch,
    )
    registry.register(_OneShotStrategy("StrategyA", "ACC_A", idempotency_key="ca-1"))
    registry.register(_OneShotStrategy("StrategyB", "ACC_B", idempotency_key="cb-1"))
    assignment.assign("StrategyA", "ACC_A")
    assignment.assign("StrategyB", "ACC_B")
    for sid in ("StrategyA", "StrategyB"):
        registry.enable(sid)
        registry.start(sid)

    outcomes = {}

    def worker(sid):
        outcomes[sid] = runtime.run_once(sid)

    threads = [threading.Thread(target=worker, args=(sid,)) for sid in ("StrategyA", "StrategyB")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes["StrategyA"].executions[0].account_id == "ACC_A"
    assert outcomes["StrategyB"].executions[0].account_id == "ACC_B"


# --------------------------------------------------------------------------- #
# Structural safety
# --------------------------------------------------------------------------- #
def test_module_contains_no_broker_sdk_import_or_direct_mutation_call():
    import inspect
    import trading.common.strategy_runtime as mod

    source = inspect.getsource(mod)
    for forbidden in (
        "smart_api", "SmartConnect", "import requests", "dhanhq",
        "breeze_connect", ".place_order(", ".modify_order(", ".cancel_order(",
    ):
        assert forbidden not in source, forbidden
    assert "StrategyExecutionEngine(" in source  # the ONE, intended broker-facing call site
    assert "live_authorization" not in source.lower()


def test_module_never_sets_authorization_state_or_kills_an_account():
    import inspect
    import trading.common.strategy_runtime as mod

    source = inspect.getsource(mod)
    assert "set_authorization_state" not in source
    assert "set_killed" not in source


def test_no_command_ever_authorizes_an_account_end_to_end():
    manager, registry, assignment, kill_switch, runtime = _setup()
    strategy = _OneShotStrategy(STRATEGY_ID, "ACC1")
    registry.register(strategy)
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)
    runtime.run_once(STRATEGY_ID)
    from trading.common.trading_account import AccountAuthorizationState

    assert manager.get_account("ACC1").authorization_state == AccountAuthorizationState.READ_ONLY


def test_real_broker_place_order_call_count_is_zero_with_a_recording_broker():
    """Explicit REAL BROKER SAFETY test using a recording spy: proves
    place_order()/modify_order()/cancel_order() are never invoked on the
    resolved simulation broker itself in this scenario, because dry_run
    short-circuits before them (layer 1) -- only the synthetic dry-run path
    executes."""

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
    manager, registry, assignment, kill_switch, runtime = _setup(broker_client=recording)
    strategy = _OneShotStrategy(STRATEGY_ID, "ACC1")
    registry.register(strategy)
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)

    result = runtime.run_once(STRATEGY_ID)

    assert result.executions[0].success is True
    assert recording.place_order_calls == 0
    assert recording.modify_order_calls == 0
    assert recording.cancel_order_calls == 0
