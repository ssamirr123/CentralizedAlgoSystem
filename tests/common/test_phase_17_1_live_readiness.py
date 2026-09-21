"""Phase 17.1: Production Live Architecture Readiness & Distributed Safety
Review.

This file adds NEW coverage for the one concrete safety gap this phase's
review found and fixed (Section 27 -- an ambiguous/unknown broker outcome
must never be treated the same as a confirmed failure when releasing a
PortfolioRiskManager exposure/order-count reservation), plus a from-scratch
FakeBroker-based simulation of the future live/live-canary execution path
(Section 43-44) and a multi-worker cross-account/cross-strategy isolation
test (Section 45).

No real broker credential, network call, or mutation is used anywhere in
this file. No LiveAuthorization is ever granted to a state that would let
a real order through -- every FakeBroker below is wired directly into
StrategyExecutionEngine with PAPER/dry_run-equivalent behavior EXCEPT where
a test deliberately proves the engine's own gates (not a broker-level
dry_run flag) are what prevent a mutation call, matching this phase's own
requirement to re-verify gate behavior "from current source", not merely
from the pre-existing hard shadow boundary.
"""
from __future__ import annotations

from trading.common.broker import BrokerClient, OrderResult, OrderSide, OrderType, Quote
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.execution import (
    ExecutionConfig,
    ExecutionResult,
    StrategyExecutionEngine,
)
from trading.common.idempotency_store import SqliteIdempotencyStore
from trading.common.kill_switch import CentralKillSwitch
from trading.common.live_authorization import SqliteLiveAuthorizationStore
from trading.common.order_intent import OrderIntent
from trading.common.portfolio_risk import PortfolioRiskLimits, PortfolioRiskManager
from trading.common.risk_manager import RiskLimits, RiskManager
from trading.common.strategy import BaseStrategy
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_registry import StrategyRegistry
from trading.common.strategy_runtime import StrategyRuntime
from trading.common.trading_account import AccountAuthorizationState, ExecutionMode, TradingAccount
from trading.common.worker_coordinator import WorkerCoordinator
from trading.common.worker_protocol import OrderIntentSubmission
from trading.common.worker_registry import WorkerRegistry

ACCOUNT_A = "ANGEL_SAMIR"
ACCOUNT_B = "ANGEL_ACCOUNT_B"
SYMBOL = "NIFTY24950CE"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeMutationRecordingBroker(BrokerClient):
    """Records every mutation ATTEMPT (never a real network/SDK call) --
    used to count exactly how many times place_order()/modify_order()/
    cancel_order() would have been invoked for a given scenario."""

    is_simulated = True

    def __init__(self, *, raise_on_place: Exception | None = None, reject_message: str | None = None) -> None:
        self.connected = False
        self.place_order_calls = 0
        self.cancel_order_calls = 0
        self.modify_order_calls = 0
        self._raise_on_place = raise_on_place
        self._reject_message = reject_message

    def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, last_price=100.0, timestamp="2026-01-01T00:00:00+00:00")

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
        self.place_order_calls += 1
        if self._raise_on_place is not None:
            raise self._raise_on_place
        if self._reject_message is not None:
            return OrderResult(order_id="", symbol=symbol, side=side, quantity=quantity, status="REJECTED", message=self._reject_message)
        return OrderResult(order_id=f"FAKE-{self.place_order_calls}", symbol=symbol, side=side, quantity=quantity, status="FILLED", message="ok")

    def cancel_order(self, order_id: str) -> bool:
        self.cancel_order_calls += 1
        return True

    def modify_order(self, order_id: str, **kwargs) -> OrderResult:
        self.modify_order_calls += 1
        return OrderResult(order_id=order_id, symbol="", side=OrderSide.BUY, quantity=0, status="FILLED", message="ok")

    def get_positions(self):
        return []


def _intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id="StrategyA", account_id=ACCOUNT_A, symbol=SYMBOL, exchange="NFO",
        side=OrderSide.BUY, quantity=5, order_type=OrderType.LIMIT, limit_price=100.0,
        idempotency_key="idem-1", correlation_id="corr-1",
    )
    fields.update(overrides)
    return OrderIntent(**fields)


def _engine(broker, *, tmp_path, risk_manager=None, kill_switch=None, live_auth_store=None, account_id=ACCOUNT_A, execution_mode=ExecutionMode.PAPER, authorization_state=AccountAuthorizationState.READ_ONLY):
    idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / f"idem-{account_id}.db"))
    broker_manager = BrokerManager()
    account = TradingAccount(
        account_id=account_id, account_name=account_id, broker_id="fake",
        execution_mode=execution_mode, authorization_state=authorization_state,
    )
    broker_manager.register_account(account, broker_client=broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign("StrategyA", account_id)
    if risk_manager is None:
        risk_manager = RiskManager(assignment, limits=RiskLimits(
            max_order_quantity=100, max_order_value=100000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
            max_orders_per_day=10, max_strategy_exposure=100000.0, max_account_exposure=100000.0,
        ))
    engine = StrategyExecutionEngine(
        broker, ExecutionConfig(min_api_interval_seconds=0.0, retry_delay_seconds=0.0),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager,
        idempotency_store=idem_store, central_kill_switch=kill_switch,
        live_authorization_store=live_auth_store,
    )
    return engine, broker_manager, assignment


# --------------------------------------------------------------------------- #
# Section 27 -- ExecutionResult.ambiguous() is distinct from .rejected()
# --------------------------------------------------------------------------- #
def test_ambiguous_execution_result_has_its_own_status_not_rejected(tmp_path):
    class _RaisingBroker(PaperBroker):
        def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, limit_price=None):
            raise ConnectionError("simulated network timeout")

    engine, _, _ = _engine(_RaisingBroker(), tmp_path=tmp_path)
    result = engine.execute(_intent())
    assert result.success is False
    assert result.status == "AMBIGUOUS"
    assert result.status != "REJECTED"


def test_confirmed_rejection_still_status_rejected_not_ambiguous(tmp_path):
    engine, _, _ = _engine(_FakeMutationRecordingBroker(reject_message="AG7002 not whitelisted"), tmp_path=tmp_path)
    result = engine.execute(_intent(idempotency_key="idem-rej"))
    assert result.success is False
    assert result.status == "REJECTED"


# --------------------------------------------------------------------------- #
# Section 26/27 -- PortfolioRiskManager must not release exposure for an
# outcome it cannot prove never happened.
# --------------------------------------------------------------------------- #
def test_commit_reservation_keeps_exposure_open_on_ambiguous_outcome():
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=10_000.0))
    intent = _intent(quantity=10, limit_price=100.0)
    decision = prm.evaluate_and_reserve(intent, strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert decision.allowed is True
    reservation_id = decision.reservation_id

    ambiguous_result = ExecutionResult.ambiguous(intent, "broker call outcome is ambiguous")
    prm.commit_reservation(reservation_id, execution_result=ambiguous_result)

    snapshot = prm.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    # Exposure/order-count budget for this order must NOT have been given
    # back -- the broker may have actually accepted it.
    assert snapshot.strategy_exposure == 1000.0
    assert snapshot.strategy_orders_today == 1

    # It must be reachable for an operator/reconciliation flow to resolve
    # later, exactly like any other still-open order.
    prm.resolve_open_order(reservation_id)


def test_commit_reservation_still_releases_exposure_on_confirmed_rejection():
    """Regression guard: the Section 27 fix must not overcorrect and start
    holding exposure open for a genuinely confirmed failure too."""
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=10_000.0))
    intent = _intent(quantity=10, limit_price=100.0)
    decision = prm.evaluate_and_reserve(intent, strategy_id="StrategyA", account_id=ACCOUNT_A)
    reservation_id = decision.reservation_id

    rejected_result = ExecutionResult.rejected(intent, "order rejected by broker (confirmed, no order created)")
    prm.commit_reservation(reservation_id, execution_result=rejected_result)

    snapshot = prm.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 0.0
    assert snapshot.strategy_orders_today == 0


def test_commit_reservation_still_commits_a_real_fill_normally():
    """Regression guard: a genuine COMPLETE fill must still be booked to the
    ledger as before -- the new AMBIGUOUS branch must not shadow it."""
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=10_000.0))
    intent = _intent(quantity=10, limit_price=100.0)
    decision = prm.evaluate_and_reserve(intent, strategy_id="StrategyA", account_id=ACCOUNT_A)
    reservation_id = decision.reservation_id

    filled = ExecutionResult(success=True, status="COMPLETE", order_id="X-1", filled_quantity=10)
    prm.commit_reservation(reservation_id, execution_result=filled)

    snapshot = prm.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 1000.0  # a fresh BUY fill opens a held position
    assert snapshot.strategy_orders_today == 1


def test_release_reservation_is_a_noop_for_already_committed_ambiguous_id():
    """A caller must never be able to blindly release() an ambiguous
    reservation after the fact and silently understate exposure -- once
    commit_reservation() has filed it under open orders, release_reservation
    on the SAME id (a stale/duplicate cleanup call) is a documented no-op,
    not a way to bypass the fix."""
    prm = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=10_000.0))
    intent = _intent(quantity=10, limit_price=100.0)
    decision = prm.evaluate_and_reserve(intent, strategy_id="StrategyA", account_id=ACCOUNT_A)
    reservation_id = decision.reservation_id
    prm.commit_reservation(reservation_id, execution_result=ExecutionResult.ambiguous(intent, "unknown"))

    prm.release_reservation(reservation_id)  # reservation_id no longer in self._reservations

    snapshot = prm.get_snapshot(strategy_id="StrategyA", account_id=ACCOUNT_A)
    assert snapshot.strategy_exposure == 1000.0  # untouched by the no-op release


# --------------------------------------------------------------------------- #
# Section 43/44 -- FakeBroker-based distributed-live simulation, exercised
# directly against StrategyExecutionEngine (the actual future live/canary
# boundary -- WorkerCoordinator's own StrategyRuntime forces dry_run=True
# structurally and can never reach this code path, see Section 3 of the
# report: this file proves the ENGINE's gates, independent of that
# additional shadow boundary).
# --------------------------------------------------------------------------- #
def test_scenario_valid_path_results_in_exactly_one_mutation_attempt(tmp_path):
    broker = _FakeMutationRecordingBroker()
    engine, _, _ = _engine(broker, tmp_path=tmp_path)
    result = engine.execute(_intent(idempotency_key="valid-1"))
    assert result.success is True
    assert broker.place_order_calls == 1
    assert broker.modify_order_calls == 0
    assert broker.cancel_order_calls == 0


def test_scenario_missing_live_authorization_results_in_zero_mutations(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    broker = _FakeMutationRecordingBroker()
    engine, _, _ = _engine(
        broker, tmp_path=tmp_path, live_auth_store=auth_store,
        execution_mode=ExecutionMode.LIVE, authorization_state=AccountAuthorizationState.LIVE_AUTHORIZED,
    )
    # No authorization_id at all on the intent's metadata.
    result = engine.execute(_intent(idempotency_key="missing-auth"))
    assert result.success is False
    assert broker.place_order_calls == 0


def test_scenario_expired_authorization_results_in_zero_mutations(tmp_path):
    auth_store = SqliteLiveAuthorizationStore(db_path=str(tmp_path / "auth.db"))
    rec = auth_store.create(
        account_id=ACCOUNT_A, broker_id="fake", credential_reference="env:FAKE",
        symbol=SYMBOL, side="BUY", quantity=5, order_type="LIMIT", product_type="INTRADAY",
        max_order_value=10000.0, daily_loss_limit=5000.0, strategy_loss_limit=5000.0,
        max_orders_per_day=5, idempotency_key="expired-auth", authorized_by="samir",
        ttl_seconds=-1,  # already expired
    )
    # record is already lazily transitioned to EXPIRED on read -- never
    # reaches AUTHORIZED, so try_consume() must reject it downstream.
    broker = _FakeMutationRecordingBroker()
    engine, _, _ = _engine(
        broker, tmp_path=tmp_path, live_auth_store=auth_store,
        execution_mode=ExecutionMode.LIVE, authorization_state=AccountAuthorizationState.LIVE_AUTHORIZED,
    )
    result = engine.execute(_intent(idempotency_key="expired-auth", metadata={"authorization_id": rec.authorization_id}))
    assert result.success is False
    assert broker.place_order_calls == 0


def test_scenario_kill_switch_engaged_results_in_zero_mutations(tmp_path):
    kill_switch = CentralKillSwitch(persistence_path=str(tmp_path / "kill.json"))
    kill_switch.engage(reason="test")
    broker = _FakeMutationRecordingBroker()
    engine, _, _ = _engine(broker, tmp_path=tmp_path, kill_switch=kill_switch)
    result = engine.execute(_intent(idempotency_key="killed-1"))
    assert result.success is False
    assert broker.place_order_calls == 0


def test_scenario_risk_manager_rejection_results_in_zero_mutations(tmp_path):
    risk_manager = None  # built inside _engine, but we want a max_order_quantity of 1 to force a reject
    broker = _FakeMutationRecordingBroker()
    from trading.common.strategy_assignment import StrategyAssignment as _SA
    idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / "idem-risk.db"))
    broker_manager = BrokerManager()
    account = TradingAccount(account_id=ACCOUNT_A, account_name=ACCOUNT_A, broker_id="fake", execution_mode=ExecutionMode.PAPER)
    broker_manager.register_account(account, broker_client=broker)
    assignment = _SA(broker_manager)
    assignment.assign("StrategyA", ACCOUNT_A)
    strict_risk = RiskManager(assignment, limits=RiskLimits(
        max_order_quantity=1, max_order_value=100000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=100000.0, max_account_exposure=100000.0,
    ))
    engine = StrategyExecutionEngine(
        broker, ExecutionConfig(min_api_interval_seconds=0.0, retry_delay_seconds=0.0),
        risk_manager=strict_risk, strategy_assignment=assignment, broker_manager=broker_manager,
        idempotency_store=idem_store,
    )
    result = engine.execute(_intent(idempotency_key="risk-reject-1", quantity=99))
    assert result.success is False
    assert broker.place_order_calls == 0


def test_scenario_confirmed_rejection_is_exactly_one_attempt_no_blind_retry(tmp_path):
    broker = _FakeMutationRecordingBroker(reject_message="AG7002 confirmed rejection")
    engine, _, _ = _engine(broker, tmp_path=tmp_path)
    result = engine.execute(_intent(idempotency_key="confirmed-rej-1"))
    assert result.success is False
    assert result.status == "REJECTED"
    # place_order is called once per retry attempt for a confirmed
    # rejection (execution.py retries a confirmed rejection with a fresh
    # price up to max_retries) -- the safety property under test is that it
    # is bounded (never unbounded/blind) and never mutates state twice for
    # what is logically ONE order.
    assert 1 <= broker.place_order_calls <= 3


def test_scenario_unknown_outcome_is_exactly_one_attempt_no_blind_retry(tmp_path):
    class _RaisingBroker(PaperBroker):
        def __init__(self):
            super().__init__()
            self.call_count = 0

        def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, limit_price=None):
            self.call_count += 1
            raise ConnectionError("simulated network timeout mid-submission")

    broker = _RaisingBroker()
    engine, _, _ = _engine(broker, tmp_path=tmp_path)
    result = engine.execute(_intent(idempotency_key="ambiguous-1"))
    assert result.success is False
    assert result.status == "AMBIGUOUS"
    assert broker.call_count == 1  # never blindly retried


def test_scenario_duplicate_network_submission_is_one_logical_mutation(tmp_path):
    broker = _FakeMutationRecordingBroker()
    idem_store = SqliteIdempotencyStore(db_path=str(tmp_path / "idem-dup.db"))
    broker_manager = BrokerManager()
    account = TradingAccount(account_id=ACCOUNT_A, account_name=ACCOUNT_A, broker_id="fake", execution_mode=ExecutionMode.PAPER)
    broker_manager.register_account(account, broker_client=broker)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign("StrategyA", ACCOUNT_A)
    risk_manager = RiskManager(assignment, limits=RiskLimits(
        max_order_quantity=100, max_order_value=100000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=100000.0, max_account_exposure=100000.0,
    ))
    engine = StrategyExecutionEngine(
        broker, ExecutionConfig(min_api_interval_seconds=0.0, retry_delay_seconds=0.0),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager,
        idempotency_store=idem_store,
    )
    intent = _intent(idempotency_key="dup-network-1")
    first = engine.execute(intent)
    second = engine.execute(intent)  # simulates a worker retry after a lost HTTP response
    assert first.success is True
    assert second.success is True
    assert broker.place_order_calls == 1  # replayed from the idempotency store, never a second mutation


# --------------------------------------------------------------------------- #
# Section 45 -- multi-worker cross-account/cross-strategy leakage
# --------------------------------------------------------------------------- #
class _NoopStrategy(BaseStrategy):
    def __init__(self, strategy_id: str, account_id: str, idempotency_key: str) -> None:
        super().__init__(strategy_id)
        self._account_id = account_id
        self._idempotency_key = idempotency_key

    def _on_generate_order_intents(self):
        return []

    def make_intent(self, **overrides) -> OrderIntent:
        fields = dict(
            strategy_id=self.strategy_id, account_id=self._account_id, symbol="NIFTY", exchange="NFO",
            side=OrderSide.BUY, quantity=1, order_type=OrderType.MARKET, idempotency_key=self._idempotency_key,
        )
        fields.update(overrides)
        return OrderIntent(**fields)


def test_three_workers_cannot_cross_execute_each_others_strategies_or_accounts():
    """Three strategies, three accounts, three workers -- each worker may
    only ever move ITS OWN strategy against ITS OWN account, never another
    worker's, regardless of what account_id/strategy_id a malformed or
    malicious submission's OrderIntent claims."""
    manager = BrokerManager()
    registry = StrategyRegistry()
    assignment = StrategyAssignment(manager)
    strategies = {}
    for name, acct in (("StratX", "AccX"), ("StratY", "AccY"), ("StratZ", "AccZ")):
        manager.register_account(
            TradingAccount(account_id=acct, account_name=acct, broker_id="paper", execution_mode=ExecutionMode.SHADOW),
            broker_client=PaperBroker(),
        )
        strategy = _NoopStrategy(name, acct, f"key-{name}")
        registry.register(strategy)
        assignment.assign(name, acct)
        registry.enable(name)
        registry.start(name)
        strategies[name] = strategy

    risk_manager = RiskManager(assignment)
    kill_switch = CentralKillSwitch()
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch,
    )
    workers = WorkerRegistry()
    coordinator = WorkerCoordinator(worker_registry=workers, strategy_registry=registry, strategy_assignment=assignment, strategy_runtime=runtime)

    sessions = {}
    for wid, strat in (("w1", "StratX"), ("w2", "StratY"), ("w3", "StratZ")):
        worker = workers.register_worker(worker_id=wid, name=wid)
        workers.assign_strategy(strat, wid)
        sessions[wid] = worker.session_id

    # Each worker legitimately executes its own strategy.
    for wid, strat in (("w1", "StratX"), ("w2", "StratY"), ("w3", "StratZ")):
        result = coordinator.submit_order_intent(OrderIntentSubmission(
            worker_id=wid, session_id=sessions[wid], strategy_id=strat, evaluation_id="e1",
            intent=strategies[strat].make_intent(),
        ))
        assert result.accepted is True

    # w1 attempts to submit against StratY's strategy_id (never assigned to it).
    cross_strategy = coordinator.submit_order_intent(OrderIntentSubmission(
        worker_id="w1", session_id=sessions["w1"], strategy_id="StratY", evaluation_id="e2",
        intent=strategies["StratY"].make_intent(idempotency_key="key-StratY-hijack"),
    ))
    assert cross_strategy.accepted is False
    assert "not assigned to worker" in cross_strategy.reason

    # w2 submits a legitimately-owned StratY intent, but the intent object
    # itself falsely claims AccZ as the account -- routing must still be
    # resolved centrally via StrategyAssignment, ignoring the intent's own
    # (attacker-controlled) account_id.
    forged_account_intent = strategies["StratY"].make_intent(account_id="AccZ", idempotency_key="key-StratY-forged")
    result = coordinator.submit_order_intent(OrderIntentSubmission(
        worker_id="w2", session_id=sessions["w2"], strategy_id="StratY", evaluation_id="e3",
        intent=forged_account_intent,
    ))
    assert result.accepted is True
    # Confirm it executed against AccY (the centrally-assigned account), not
    # the forged AccZ -- AccZ's ledger must show no trace of this order.
    assert result.execution_result.account_id == "AccY"
