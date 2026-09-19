"""
StrategyRuntime -- Phase 16.5: the seam that finally connects a registered
Strategy's decision logic to the broker-agnostic execution architecture,
in a PAPER/SHADOW-only, non-mutating way:

    Trading Control Center
              |
              v
    Strategy Control (Phase 16.4 START/STOP -- unchanged)
              |
              v
    Strategy Runtime (THIS MODULE)
              |
              v
    Strategy.generate_order_intents()  (Phase 10, unchanged interface)
              |
              v
    OrderIntent  (Phase 1, unchanged)
              |
              v
    StrategyExecutionEngine.execute()  (Phases 1-15D, UNCHANGED gate order)
              |
              v
    PAPER / SHADOW broker (structurally non-networked -- see below)

WHAT THIS MODULE DOES NOT DO (by construction, not just configuration):
  - It never imports a broker SDK, never holds a broker credential, and
    never calls place_order/modify_order/cancel_order directly -- the only
    broker-facing call anywhere in this file is StrategyExecutionEngine
    .execute(intent), the exact same call any other caller of that engine
    already makes.
  - It never creates or consumes a live trading authorization, and never
    changes an account's own authorization/kill state in any way.
  - It never starts a real process/thread/scheduler on its own -- run_once()
    is a single, explicit, synchronous evaluation cycle. Nothing in this
    module spawns a background loop or automatically retries/restarts a
    failed strategy; see run_once()'s own docstring.

HARD SHADOW BOUNDARY (three independent, layered guarantees):
  1. This class's own StrategyExecutionEngine is constructed with
     ExecutionConfig(dry_run=True), HARD-CODED and not exposed as a
     constructor parameter of this class -- engine.execute()'s own
     place_limit()/place_market_emergency()/cancel() short-circuit BEFORE
     ever calling a broker's place_order()/cancel_order(), regardless of
     which broker class is resolved for the account.
  2. Before handing an intent to the engine, this module independently
     resolves the account's broker and requires it to be one of the
     known-structurally-safe simulation implementations (PaperBroker,
     ShadowBroker, ConnectedShadowBroker) -- see _assert_simulated_broker().
     Anything else is refused outright (ShadowBoundaryViolation), and the
     strategy is marked ERROR rather than silently skipped.
  3. trading/api/execution_state.py (Phase 16.5) now wires every demo
     account's BrokerClient to an eager, structurally-non-networked
     ShadowBroker() -- closing a latent gap this phase's own READ step
     found: those accounts previously used a LAZY factory that would have
     constructed a REAL AngelOneBroker/DhanBroker/ICICIBreezeBroker
     (config-set to "paper" trading_mode) the first time anything called
     BrokerManager.get_broker() on them, which nothing did until this
     runtime existed to make that call.

Any one of these three would already prevent a real broker mutation; all
three exist so that no single config mistake is sufficient to reach one.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from trading.common.broker_manager import BrokerManager
from trading.common.brokers.connected_shadow_broker import ConnectedShadowBroker
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.brokers.shadow_broker import ShadowBroker
from trading.common.execution import ExecutionConfig, ExecutionResult, StrategyExecutionEngine
from trading.common.idempotency_store import IdempotencyStore, InMemoryIdempotencyStore
from trading.common.kill_switch import CentralKillSwitch
from trading.common.market_data_gateway import (
    DEFAULT_MAX_DATA_AGE_SECONDS,
    MarketDataSource,
    MarketDataStatus,
    gather_market_data,
)
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskManager
from trading.common.strategy import StrategyStatus
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_registry import StrategyRegistry, UnknownStrategyError

__all__ = [
    "RuntimeState", "RuntimeCycleResult", "RuntimeStatus", "StrategyRuntime",
    "ShadowBoundaryViolation", "UnknownStrategyError",
]

# The complete, closed allowlist of BrokerClient implementations this
# runtime will ever hand an OrderIntent to. Every member of this tuple is
# structurally incapable of a real network call (see each class's own
# module docstring) -- adding a real adapter here would defeat the entire
# point of this list, so it is intentionally not configurable.
_SIMULATED_BROKER_TYPES = (PaperBroker, ShadowBroker, ConnectedShadowBroker)

_ACTIVE_STATUSES = frozenset({StrategyStatus.RUNNING, StrategyStatus.SHADOW})


class RuntimeState(str, Enum):
    """Deliberately a FOURTH vocabulary, distinct from StrategyStatus
    (Phase 10), AccountAuthorizationState (Phase 15B), and LifecycleState
    (Phase 16.3) -- this one describes the RUNTIME's own health, not the
    strategy's configuration state or the account's trust level."""

    INACTIVE = "INACTIVE"  # strategy is not currently RUNNING/SHADOW -- nothing to evaluate
    HEALTHY = "HEALTHY"    # strategy is active and its last cycle (if any) completed cleanly
    FAILED = "FAILED"      # the last cycle raised, or the hard shadow boundary refused to proceed


class ShadowBoundaryViolation(RuntimeError):
    """Raised internally when a resolved broker is not a known-simulated
    BrokerClient, or when an OrderIntent is otherwise inconsistent with the
    strategy being run. Always caught inside run_once() and turned into a
    FAILED RuntimeState + a strategy ERROR transition -- never left to
    propagate to a caller, and never a path to a broker call."""


@dataclass
class RuntimeCycleResult:
    strategy_id: str
    ticked: bool  # False when the strategy wasn't RUNNING/SHADOW -- a pure no-op, nothing evaluated
    intents_generated: int
    executions: list[ExecutionResult] = field(default_factory=list)
    error: str = ""
    # Phase 16.6 -- "" when the strategy declares no required_instruments()
    # (Phase 16.5 behavior, unchanged); otherwise one of MarketDataStatus's
    # values. NO_DATA/STALE/INVALID/PROVIDER_ERROR all mean
    # generate_order_intents() was never called this cycle -- fail closed,
    # not a strategy/runtime failure.
    market_data_status: str = ""


@dataclass
class RuntimeStatus:
    strategy_id: str
    state: RuntimeState
    last_heartbeat_at: str
    last_intent_at: str
    last_cycle_at: str
    last_error: str
    last_result_summary: str
    market_data_status: str = ""
    last_market_data_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrategyRuntime:
    """Owns exactly one StrategyExecutionEngine (dry_run=True, hard-coded)
    and exposes run_once()/get_status() per strategy_id. Holds no broker
    credential, no LiveAuthorization, no risk policy, and no kill-switch
    logic of its own -- all of those are the pre-existing objects it is
    handed at construction time, used completely unmodified."""

    def __init__(
        self,
        *,
        strategy_registry: StrategyRegistry,
        strategy_assignment: StrategyAssignment,
        broker_manager: BrokerManager,
        risk_manager: RiskManager,
        kill_switch: CentralKillSwitch,
        metrics_registry: Any | None = None,
        audit_trail: Any | None = None,
        idempotency_store: IdempotencyStore | None = None,
        market_data_source: MarketDataSource | None = None,
        max_data_age_seconds: float = DEFAULT_MAX_DATA_AGE_SECONDS,
    ) -> None:
        self._strategy_registry = strategy_registry
        self._strategy_assignment = strategy_assignment
        self._broker_manager = broker_manager
        self._metrics_registry = metrics_registry
        # Phase 16.6 -- optional. None (the default) preserves Phase
        # 16.5's exact behavior for every strategy, since
        # gather_market_data() with no source configured only matters for
        # a strategy that actually declares required_instruments() (none
        # of the three registered production strategies do today).
        self._market_data_source = market_data_source
        self._max_data_age_seconds = max_data_age_seconds
        self._last_market_data_status: dict[str, str] = {}
        self._last_market_data_at: dict[str, str] = {}
        # Reuses the existing idempotency-store abstraction (Phase 15D --
        # trading/common/idempotency_store.py), never a second, unrelated
        # mechanism. InMemoryIdempotencyStore is explicitly documented as
        # non-production (does not survive a restart) -- acceptable here
        # because this entire runtime is PAPER/SHADOW-only by construction;
        # a caller wanting durable shadow-run idempotency may pass a
        # SqliteIdempotencyStore instead.
        self._idempotency_store = idempotency_store or InMemoryIdempotencyStore()
        # Layer 1 of the hard shadow boundary -- see module docstring. The
        # broker passed here (PaperBroker()) is only ever this engine's
        # OWN fallback default; execute() always re-resolves the real
        # per-account broker via broker_manager for every intent (see
        # StrategyExecutionEngine.execute()'s own broker-resolution step),
        # so this is not a routing decision, just a safe non-None default.
        self._engine = StrategyExecutionEngine(
            PaperBroker(), ExecutionConfig(dry_run=True),
            risk_manager=risk_manager, strategy_assignment=strategy_assignment,
            broker_manager=broker_manager, metrics=metrics_registry,
            audit_trail=audit_trail, central_kill_switch=kill_switch,
            idempotency_store=self._idempotency_store,
        )
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._last_cycle_at: dict[str, str] = {}
        self._last_error: dict[str, str] = {}
        self._last_results: dict[str, list[ExecutionResult]] = {}
        self._failed: set[str] = set()

    def _lock_for(self, strategy_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(strategy_id, threading.Lock())

    def run_once(self, strategy_id: str) -> RuntimeCycleResult:
        """One explicit, synchronous evaluation cycle: generate this
        strategy's current OrderIntents (if any) and run each through the
        existing, unmodified execution engine. Never spawns a thread,
        never loops, never retries on its own, and never auto-restarts a
        failed strategy -- a caller (an operator action, or a test) decides
        when/whether to call this again. Raises UnknownStrategyError for an
        unregistered strategy_id; every other failure mode is reported via
        the returned RuntimeCycleResult, never an exception."""
        strategy = self._strategy_registry.get(strategy_id)  # raises UnknownStrategyError

        with self._lock_for(strategy_id):
            if strategy.get_status() not in _ACTIVE_STATUSES:
                return RuntimeCycleResult(strategy_id=strategy_id, ticked=False, intents_generated=0)

            required = strategy.required_instruments()
            market_data = None
            md_status = ""
            if required:
                market_data, md_results = gather_market_data(
                    self._market_data_source, required, max_age_seconds=self._max_data_age_seconds,
                )
                # Report the worst status across required instruments when
                # data isn't fully available; AVAILABLE only when every
                # one of them resolved AVAILABLE (gather_market_data's own
                # fail-closed, no-partial-data rule).
                md_status = (
                    MarketDataStatus.AVAILABLE.value if market_data is not None
                    else next((r.status.value for r in md_results if r.status != MarketDataStatus.AVAILABLE), "")
                )
                self._last_market_data_status[strategy_id] = md_status
                if market_data is not None:
                    self._last_market_data_at[strategy_id] = _now()
                if market_data is None:
                    # Fail closed: NO_DATA/STALE/INVALID/PROVIDER_ERROR all
                    # mean generate_order_intents() is never called this
                    # cycle -- this is expected, routine behavior (e.g.
                    # outside market hours), never a strategy/runtime
                    # failure, so RuntimeState stays HEALTHY, not FAILED.
                    self._last_cycle_at[strategy_id] = _now()
                    self._failed.discard(strategy_id)
                    self._last_error.pop(strategy_id, None)
                    if self._metrics_registry is not None:
                        self._metrics_registry.record_strategy_heartbeat(strategy_id)
                    return RuntimeCycleResult(
                        strategy_id=strategy_id, ticked=True, intents_generated=0,
                        market_data_status=md_status,
                    )

            try:
                intents = strategy.generate_order_intents(market_data)
            except Exception as exc:
                strategy.mark_error(f"strategy_runtime: STRATEGY_EVALUATION_FAILED: {exc}")
                self._last_error[strategy_id] = str(exc)
                self._failed.add(strategy_id)
                self._last_cycle_at[strategy_id] = _now()
                return RuntimeCycleResult(
                    strategy_id=strategy_id, ticked=True, intents_generated=0, error=str(exc),
                    market_data_status=md_status,
                )

            executions: list[ExecutionResult] = []
            error = ""
            try:
                for intent in intents:
                    if intent.strategy_id != strategy_id:
                        raise ShadowBoundaryViolation(
                            f"OrderIntent.strategy_id={intent.strategy_id!r} does not match "
                            f"the strategy being run ({strategy_id!r}) -- refusing to execute"
                        )
                    self._assert_simulated_broker(intent)
                    executions.append(self._engine.execute(intent))
            except ShadowBoundaryViolation as exc:
                strategy.mark_error(f"strategy_runtime: SHADOW_EXECUTION_FAILED: {exc}")
                error = str(exc)
            except Exception as exc:
                # e.g. IdempotencyKeyReuseError -- execute() deliberately
                # RAISES (rather than returning a rejected ExecutionResult)
                # for a caller-side bug like reusing one idempotency_key
                # for two genuinely different intents. Never let that
                # escape run_once() uncaught -- fail closed, report it,
                # same as every other cycle failure mode.
                strategy.mark_error(f"strategy_runtime: SHADOW_EXECUTION_FAILED: {exc}")
                error = str(exc)

            self._last_cycle_at[strategy_id] = _now()
            self._last_results[strategy_id] = executions
            if error:
                self._last_error[strategy_id] = error
                self._failed.add(strategy_id)
            else:
                self._failed.discard(strategy_id)
                self._last_error.pop(strategy_id, None)
            if self._metrics_registry is not None:
                self._metrics_registry.record_strategy_heartbeat(strategy_id)

            return RuntimeCycleResult(
                strategy_id=strategy_id, ticked=True, intents_generated=len(intents),
                executions=executions, error=error, market_data_status=md_status,
            )

    def is_strategy_active(self, strategy_id: str) -> bool:
        """Phase 16.9: public, read-only check reused by WorkerCoordinator
        (trading/common/worker_coordinator.py) to confirm a strategy's
        lifecycle permits evaluation before accepting a worker's
        OrderIntent submission -- the exact same status set run_once()
        itself gates on, never a second definition of "active"."""
        return self._strategy_registry.get(strategy_id).get_status() in _ACTIVE_STATUSES

    def execute_worker_intent(self, intent: OrderIntent, *, owning_strategy_id: str) -> ExecutionResult:
        """Phase 16.9: the ONLY seam a WorkerCoordinator may call to turn a
        worker-submitted OrderIntent into an executed result. Reuses this
        SAME instance's engine and hard shadow boundary
        (_assert_simulated_broker) -- never a second execution engine or
        a second boundary check. `owning_strategy_id` is the strategy the
        CENTRAL coordinator has already authoritatively determined this
        submission belongs to (after validating worker/assignment
        ownership) -- this method independently re-confirms the intent's
        own strategy_id agrees, exactly like run_once()'s per-intent
        check, and raises ShadowBoundaryViolation (never executes) on any
        mismatch or non-simulated broker."""
        if intent.strategy_id != owning_strategy_id:
            raise ShadowBoundaryViolation(
                f"OrderIntent.strategy_id={intent.strategy_id!r} does not match "
                f"the owning strategy ({owning_strategy_id!r}) -- refusing to execute"
            )
        self._assert_simulated_broker(intent)
        return self._engine.execute(intent)

    def _assert_simulated_broker(self, intent: OrderIntent) -> None:
        """Layer 2 of the hard shadow boundary -- see module docstring.
        Resolution mirrors exactly what execute() itself does (via
        StrategyAssignment, never intent.account_id, which is informational
        only), so this can never disagree with what the engine would
        actually route to."""
        try:
            account_id = self._strategy_assignment.get_account_id(intent.strategy_id)
            broker = self._broker_manager.get_broker(account_id)
        except Exception as exc:
            raise ShadowBoundaryViolation(f"cannot resolve a broker for this intent: {exc}") from exc
        if not isinstance(broker, _SIMULATED_BROKER_TYPES):
            allowed = ", ".join(t.__name__ for t in _SIMULATED_BROKER_TYPES)
            raise ShadowBoundaryViolation(
                f"resolved broker {type(broker).__name__!r} is not a known-simulated broker "
                f"(expected one of: {allowed}); refusing to execute"
            )

    def get_status(self, strategy_id: str) -> RuntimeStatus:
        strategy = self._strategy_registry.get(strategy_id)  # raises UnknownStrategyError

        last_heartbeat_at = ""
        if self._metrics_registry is not None:
            ts = self._metrics_registry.snapshot().gauges.get(f"strategy_heartbeat:{strategy_id}")
            if ts:
                last_heartbeat_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

        if strategy_id in self._failed:
            state = RuntimeState.FAILED
        elif strategy.get_status() in _ACTIVE_STATUSES:
            state = RuntimeState.HEALTHY
        else:
            state = RuntimeState.INACTIVE

        results = self._last_results.get(strategy_id, [])
        summary = ""
        if results:
            last = results[-1]
            summary = f"{last.status or ('EXECUTED' if last.success else 'REJECTED')}"

        return RuntimeStatus(
            strategy_id=strategy_id, state=state, last_heartbeat_at=last_heartbeat_at,
            last_intent_at=strategy.get_metrics().last_intent_at,
            last_cycle_at=self._last_cycle_at.get(strategy_id, ""),
            last_error=self._last_error.get(strategy_id, ""), last_result_summary=summary,
            market_data_status=self._last_market_data_status.get(strategy_id, ""),
            last_market_data_at=self._last_market_data_at.get(strategy_id, ""),
        )
