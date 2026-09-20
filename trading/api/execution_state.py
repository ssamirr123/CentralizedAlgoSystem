"""
Phase 11 -- process-wide wiring of the broker-agnostic execution framework
(Phases 1-10, trading/common/*) for the control-center API to read/control.

Nothing here calls a broker, connects to anything, or places an order.
Every object built by build_execution_state() is exactly the same kind of
in-memory object the Phase 1-10 test suites already construct -- this
module only centralizes ONE instance of each per FastAPI app so
trading/api/execution_routes.py's endpoints have something to read.

State lives on `app.state.execution` (an ExecutionState), built fresh
inside create_app() -- NOT as a module-level global. This matters for test
isolation: tests/conftest.py's `app` fixture calls create_app() once per
test, so each test gets its own fresh BrokerManager/StrategyRegistry/etc.
with no risk of one test's kill-switch/assignment/strategy-lifecycle state
leaking into the next (the same class of hazard Phase 5A hit and fixed for
module-level env-var mutation).

Example accounts (ANGEL_MAIN/DHAN_MAIN/ICICI_MAIN) mirror the Phase 7
reference registry exactly: all execution_mode=SHADOW, Dhan/ICICI Breeze
marked broker-unavailable (Phase 8/9 built those adapters but neither has
been validated against a real account yet, so this phase does not change
their production-routing posture). Accounts are listed by static
TradingAccount metadata only.

Phase 16.5 fix: each account's BrokerClient used to be built via a LAZY
factory (trading.common.broker.create_broker(TradingConfig(...,
trading_mode="paper"))) -- safe only because no endpoint in
execution_routes.py ever called BrokerManager.get_broker() on it (a
CONFIGURATION-based guarantee: "paper mode" is a trading_mode setting on
a real AngelOneBroker/DhanBroker/ICICIBreezeBroker instance, not a
structural one -- see trading/common/brokers/shadow_broker.py's own
docstring for that exact distinction). Phase 16.5 introduces
trading.common.strategy_runtime.StrategyRuntime, which DOES call
get_broker() (via StrategyExecutionEngine.execute()), so that
configuration-based guarantee was no longer good enough. These accounts
now register an eager trading.common.brokers.shadow_broker.ShadowBroker()
directly instead: STRUCTURALLY incapable of a real broker call (no SDK
import, no network, no credential anywhere in that file), matching each
account's own execution_mode=SHADOW. This has zero effect on any
pre-existing test or route, since get_broker() was never called on these
accounts before this phase.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

from trading.common.alerts import AlertManager
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.shadow_broker import ShadowBroker
from trading.common.idempotency_store import IdempotencyStore, InMemoryIdempotencyStore, SqliteIdempotencyStore
from trading.common.kill_switch import CentralKillSwitch
from trading.common.live_authorization import SqliteLiveAuthorizationStore
from trading.common.live_authorization_service import AuthorizationService
from trading.common.observability import AuditTrail, MetricsRegistry
from trading.common.operational_alerts import OperationalAlertStore
from trading.common.worker_auth import WorkerAuthRegistry
from trading.common.portfolio_risk import PortfolioRiskLimits, PortfolioRiskManager
from trading.common.risk_manager import RiskManager
from trading.common.strategies.combined_vwap_nifty import CombinedVwapNiftyStrategy
from trading.common.strategies.double_straddle import DoubleStraddleStrategy
from trading.common.strategies.vwap_algo_nifty_hedge import VwapAlgoNiftyHedgeStrategy
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_registry import StrategyRegistry
from trading.common.strategy_runtime import StrategyRuntime
from trading.common.trading_account import ExecutionMode, TradingAccount
from trading.common.worker_coordinator import WorkerCoordinator
from trading.common.worker_registry import WorkerRegistry

def _default_live_authorization_store() -> SqliteLiveAuthorizationStore:
    """A per-instance, per-process-unique temp file -- NEVER the class's own
    literal default path (a fixed, CWD-relative "trading_live_authorization.db"),
    which would otherwise silently share persisted authorization state
    across every test that builds a fresh ExecutionState (tests/conftest.py's
    own `app` fixture calls create_app() once per test -- see this module's
    own docstring for why that isolation matters). Production opts into a
    REAL persistent path via LIVE_AUTHORIZATION_DB_PATH in
    build_execution_state() below, exactly like AUDIT_DB_PATH/
    KILL_SWITCH_PERSISTENCE_PATH already do."""
    return SqliteLiveAuthorizationStore(db_path=tempfile.mktemp(suffix="-live-authorization.db"))


_EXAMPLE_ACCOUNTS = (
    ("ANGEL_MAIN", "Angel One (main)", "angelone"),
    ("DHAN_MAIN", "Dhan (main)", "dhan"),
    ("ICICI_MAIN", "ICICI Breeze (main)", "icici_breeze"),
)


@dataclass
class ExecutionState:
    broker_manager: BrokerManager
    strategy_assignment: StrategyAssignment
    risk_manager: RiskManager
    strategy_registry: StrategyRegistry
    # Phase 14.6: was a locally-defined KillSwitch dataclass here, entirely
    # disconnected from trading.common.live_canary.LiveCanaryGuard's own
    # kill switch (a real gap Phase 14.5's review flagged explicitly).
    # CentralKillSwitch's public interface (engaged/engaged_by/reason/
    # engaged_at/disengaged_at, engage()/disengage()) is an exact match for
    # the old KillSwitch class, so trading/api/execution_routes.py's
    # kill-switch endpoint needed ZERO changes -- only this construction
    # site changed. A StrategyExecutionEngine built against this
    # ExecutionState and passed this SAME instance as its own
    # central_kill_switch is what makes "engage via the API" and "blocked
    # in execute()" the same switch, closing that gap.
    kill_switch: CentralKillSwitch = field(default_factory=CentralKillSwitch)
    # Phase 13 observability -- one MetricsRegistry/AuditTrail/AlertManager
    # per app instance, same test-isolation rationale as everything else
    # on this dataclass (see module docstring).
    #
    # Type hint stays `AuditTrail` for documentation purposes, but Phase
    # 15D-AUDIT's PersistentAuditTrail (trading.common.audit_store) is a
    # duck-type-compatible drop-in that build_execution_state() below may
    # construct instead -- see that function's own comment.
    metrics: MetricsRegistry = field(default_factory=MetricsRegistry)
    audit_trail: Any = field(default_factory=AuditTrail)
    alerts: AlertManager = field(default_factory=AlertManager)
    # Phase 16.5 -- see trading/common/strategy_runtime.py. Constructed
    # against this SAME broker_manager/strategy_assignment/risk_manager/
    # kill_switch, so "assigned via the API" and "executed by the runtime"
    # can never silently diverge onto separate state.
    strategy_runtime: StrategyRuntime | None = None
    # Phase 16.9 -- see trading/common/worker_registry.py and
    # worker_coordinator.py. Zero workers are ever pre-registered here;
    # this only gives the read-only /api/workers endpoints (and any
    # future local worker-simulation harness) something to read/write
    # against, sharing this SAME strategy_registry/strategy_assignment/
    # strategy_runtime -- never a second execution path.
    worker_registry: WorkerRegistry | None = None
    worker_coordinator: WorkerCoordinator | None = None
    # Phase 16.10 -- see trading/common/portfolio_risk.py. Constructed with
    # every limit unconfigured (None = not enforced, the same convention
    # RiskLimits already uses) -- this phase introduces no production
    # portfolio limit values; an operator/future admin surface configures
    # real ones. Shared into worker_coordinator below so every worker
    # submission passes through the SAME PortfolioRiskManager instance the
    # read-only /api/risk/portfolio endpoints also read.
    portfolio_risk_manager: PortfolioRiskManager | None = None
    # Phase 16.11 -- see trading/common/operational_alerts.py. A
    # presentation/classification layer only (never a second risk-decision
    # engine, never authorization) -- shared into worker_registry/
    # worker_coordinator below so worker lifecycle transitions and
    # portfolio-risk/execution outcomes all raise/resolve alerts on this
    # SAME store the read-only /api/operations/* endpoints also read.
    operational_alerts: OperationalAlertStore = field(default_factory=OperationalAlertStore)
    # Phase 16.12 -- see trading/common/worker_auth.py. Empty by default
    # (WORKER_AUTH_SECRETS unset): NO worker can authenticate against the
    # new /api/worker/* machine routes until an operator explicitly
    # provisions a secret for it -- fail-closed, no production value
    # invented. Entirely separate from Permission/RBAC and from the
    # existing CONTROL_API_KEY machine lane (trading/api/deps.py) -- see
    # that module's own docstring for why.
    worker_auth_registry: WorkerAuthRegistry = field(default_factory=WorkerAuthRegistry.from_env)
    # Phase 17.1-R Remediation F -- see trading/api/live_authorization_routes.py.
    # `idempotency_store` here is a SEPARATE instance from whatever
    # StrategyExecutionEngine/StrategyRuntime uses (that engine is not
    # wired to persistent idempotency in this process at all today, a
    # pre-existing, documented gap unchanged by this phase) -- it exists
    # only so the new /api/live-authorization/* routes can perform their
    # OWN request-time "is this idempotency_key novel" preview check
    # (validate_request()/run_preflight() in live_authorization_workflow.py
    # already require one). `live_authorization_store` and
    # `authorization_service` are the same Phase 15D.5/15D.7 objects every
    # historical canary script already used, now wired to a real HTTP
    # boundary for the first time -- see that route module's own docstring
    # for the full authenticated-operator-identity design.
    idempotency_store: IdempotencyStore = field(default_factory=InMemoryIdempotencyStore)
    live_authorization_store: SqliteLiveAuthorizationStore = field(default_factory=_default_live_authorization_store)
    authorization_service: AuthorizationService = field(default_factory=AuthorizationService)


def build_execution_state() -> ExecutionState:
    metrics = MetricsRegistry()

    # Phase 15D-AUDIT: AUDIT_DB_PATH, when set, switches the process-wide
    # audit trail from Phase 13's in-memory-only AuditTrail to a durable,
    # SQLite-backed PersistentAuditTrail that survives restart (see
    # trading/common/audit_store.py). Unset (the default) preserves the
    # exact original in-memory behavior byte-for-byte -- this matters for
    # tests/conftest.py's `app` fixture, which calls create_app() fresh per
    # test and must not leave a database file behind unless a real
    # deployment has explicitly opted in.
    audit_db_path = os.environ.get("AUDIT_DB_PATH", "").strip()
    if audit_db_path:
        from trading.common.audit_store import PersistentAuditTrail

        audit_trail: Any = PersistentAuditTrail(db_path=audit_db_path)
    else:
        audit_trail = AuditTrail()

    # Phase 15D-AUDIT: likewise for the kill switch's own restart-safety
    # (Phase 15D-DR added the capability; this is where the production
    # instance actually opts in). KILL_SWITCH_PERSISTENCE_PATH unset keeps
    # the original in-memory-only default.
    kill_switch_path = os.environ.get("KILL_SWITCH_PERSISTENCE_PATH", "").strip()
    kill_switch = CentralKillSwitch(
        persistence_path=kill_switch_path or None, audit_trail=audit_trail,
    )

    alerts = AlertManager(audit_trail=audit_trail)

    broker_manager = BrokerManager(metrics_registry=metrics, audit_trail=audit_trail, alerts=alerts)
    for account_id, account_name, broker_id in _EXAMPLE_ACCOUNTS:
        account = TradingAccount(
            account_id=account_id, account_name=account_name, broker_id=broker_id,
            execution_mode=ExecutionMode.SHADOW,
        )
        broker_manager.register_account(account, broker_client=ShadowBroker())
    broker_manager.set_broker_availability(
        "dhan", False, reason="Dhan adapter (Phase 8) not yet validated against a real account"
    )
    broker_manager.set_broker_availability(
        "icici_breeze", False, reason="ICICI Breeze adapter (Phase 9) not yet validated against a real account"
    )

    strategy_assignment = StrategyAssignment(broker_manager)
    risk_manager = RiskManager(strategy_assignment)

    strategy_registry = StrategyRegistry()
    strategy_registry.register(DoubleStraddleStrategy(metrics_registry=metrics, audit_trail=audit_trail, alerts=alerts))
    strategy_registry.register(CombinedVwapNiftyStrategy(metrics_registry=metrics, audit_trail=audit_trail, alerts=alerts))
    strategy_registry.register(VwapAlgoNiftyHedgeStrategy(metrics_registry=metrics, audit_trail=audit_trail, alerts=alerts))

    strategy_runtime = StrategyRuntime(
        strategy_registry=strategy_registry, strategy_assignment=strategy_assignment,
        broker_manager=broker_manager, risk_manager=risk_manager, kill_switch=kill_switch,
        metrics_registry=metrics, audit_trail=audit_trail,
    )

    # Phase 16.11 -- shared alert store; see trading/common/
    # operational_alerts.py. Wired with the SAME audit_trail as everything
    # else so an alert's raise/resolve is recorded there too.
    operational_alerts = OperationalAlertStore(audit_trail=audit_trail)

    # Phase 16.9 -- zero workers pre-registered; see the ExecutionState
    # field comment above for why these are constructed anyway.
    # Phase 16.12: WORKER_HEARTBEAT_TIMEOUT_SECONDS lets a deployment (or a
    # test proving restart-after-timeout semantics deterministically and
    # quickly, rather than waiting a real 30s) override the default --
    # unset keeps DEFAULT_HEARTBEAT_TIMEOUT_SECONDS (30.0) exactly as
    # every prior phase's tests already assume.
    _heartbeat_timeout_raw = os.environ.get("WORKER_HEARTBEAT_TIMEOUT_SECONDS", "").strip()
    worker_registry = WorkerRegistry(
        audit_trail=audit_trail,
        **({"heartbeat_timeout_seconds": float(_heartbeat_timeout_raw)} if _heartbeat_timeout_raw else {}),
    )
    # Phase 16.10 -- additive central gate, sitting in front of the
    # existing risk_manager above (see PortfolioRiskManager's own module
    # docstring for why it is a separate object rather than a change to
    # RiskManager). Every limit defaults to None (unenforced) -- the same
    # convention RiskLimits/PortfolioRiskLimits already use -- unless an
    # operator explicitly sets PORTFOLIO_MAX_EXPOSURE (Phase 16.12: the
    # one limit this deployment surface currently exposes, added so a
    # real distributed shadow-validation run can exercise a genuine
    # portfolio-risk rejection without reaching into process memory; no
    # production value is invented here, and no other limit is wired to
    # an env var yet -- see the Phase 16.12 report for the remaining gap).
    _portfolio_max_exposure = os.environ.get("PORTFOLIO_MAX_EXPOSURE", "").strip()
    portfolio_risk_manager = PortfolioRiskManager(
        portfolio_limits=(
            PortfolioRiskLimits(max_portfolio_exposure=float(_portfolio_max_exposure))
            if _portfolio_max_exposure else None
        ),
    )
    worker_coordinator = WorkerCoordinator(
        worker_registry=worker_registry, strategy_registry=strategy_registry,
        strategy_assignment=strategy_assignment, strategy_runtime=strategy_runtime,
        portfolio_risk_manager=portfolio_risk_manager,
        audit_trail=audit_trail, operational_alerts=operational_alerts,
    )

    # Phase 17.1-R Remediation F: IDEMPOTENCY_DB_PATH/LIVE_AUTHORIZATION_DB_PATH,
    # when set, opt this deployment into durable storage for the new
    # /api/live-authorization/* routes -- unset (the default) keeps the
    # dataclass field's own safe, test-isolated default (in-memory / a
    # fresh per-instance temp file) exactly as every other optional store
    # on this class already behaves.
    _idempotency_db_path = os.environ.get("IDEMPOTENCY_DB_PATH", "").strip()
    idempotency_store: IdempotencyStore = (
        SqliteIdempotencyStore(db_path=_idempotency_db_path) if _idempotency_db_path else InMemoryIdempotencyStore()
    )
    _live_auth_db_path = os.environ.get("LIVE_AUTHORIZATION_DB_PATH", "").strip()
    live_authorization_store = (
        SqliteLiveAuthorizationStore(db_path=_live_auth_db_path) if _live_auth_db_path
        else _default_live_authorization_store()
    )

    return ExecutionState(
        broker_manager=broker_manager,
        strategy_assignment=strategy_assignment,
        risk_manager=risk_manager,
        strategy_registry=strategy_registry,
        kill_switch=kill_switch,
        metrics=metrics,
        audit_trail=audit_trail,
        alerts=alerts,
        strategy_runtime=strategy_runtime,
        worker_registry=worker_registry,
        worker_coordinator=worker_coordinator,
        portfolio_risk_manager=portfolio_risk_manager,
        operational_alerts=operational_alerts,
        idempotency_store=idempotency_store,
        live_authorization_store=live_authorization_store,
    )
