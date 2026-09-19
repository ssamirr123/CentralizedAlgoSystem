"""
OperationsSnapshot -- Phase 16.11: a single, READ-ONLY assembly of the
existing authoritative state every operator-facing dashboard field needs.

    Authoritative State  (WorkerRegistry, StrategyRegistry,
                           StrategyAssignment, BrokerManager,
                           CentralKillSwitch, StrategyRuntime,
                           PortfolioRiskManager, deployment_info)
           |
           v
    build_operations_snapshot()   <-- THIS MODULE
           |
           v
    OperationsSnapshot  (plain, immutable dataclasses)
           |
           v
    API DTOs (trading/api/execution_routes.py)  -> React dashboard

This module NEVER mutates authoritative trading state -- it only reads
already-existing collaborators (the exact same ones ExecutionState already
holds) and reuses `trading.common.strategy_lifecycle.check_all_lifecycles`
rather than re-deriving lifecycle/runtime/market-data facts a second time.
No new "position/P&L/exposure" computation is introduced here either --
portfolio risk numbers are read verbatim from Phase 16.10's own
PortfolioRiskManager.get_snapshot() (already side-effect free).

ALERT RECONCILIATION is the one place this module writes anything, and
what it writes is deliberately NOT authoritative trading state: it updates
the (already-separate, Phase 16.11) `OperationalAlertStore` -- a
presentation/classification layer (see that module's own docstring) that
never influences a RiskManager/PortfolioRiskManager decision, never grants
or revokes authorization, and never starts/stops a strategy. Every GET of
`/api/operations/*` is safe to call as often as needed: it can only ever
raise or resolve a diagnostic alert about facts that are already true,
never make anything become true.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trading.common.broker_manager import BrokerManager
from trading.common.deployment_info import get_deployment_info
from trading.common.kill_switch import CentralKillSwitch
from trading.common.operational_alerts import AlertSeverity, OperationalAlert, OperationalAlertStore
from trading.common.portfolio_risk import PortfolioRiskManager, PortfolioRiskSnapshot
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_lifecycle import StrategyLifecycleView, check_all_lifecycles
from trading.common.strategy_registry import StrategyRegistry
from trading.common.strategy_runtime import RuntimeState, StrategyRuntime
from trading.common.worker_identity import WorkerInfo, WorkerStatus
from trading.common.worker_registry import WorkerRegistry

__all__ = [
    "AccountHealthView",
    "OperationsSnapshot",
    "SafetyView",
    "StrategyHealthView",
    "SystemHealthView",
    "WorkerHealthView",
    "build_operations_snapshot",
]

_MARKET_DATA_STALE_CODES = ("STALE",)
_MARKET_DATA_MISSING_CODES = ("NO_DATA",)
_MARKET_DATA_ERROR_CODES = ("PROVIDER_ERROR", "INVALID")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SystemHealthView:
    ready: bool
    environment: str
    app_version: str
    git_sha: str
    deployment_id: str
    started_at: str
    uptime_seconds: float
    now: str


@dataclass(frozen=True)
class WorkerHealthView:
    worker_id: str
    name: str
    status: str
    session_id: str
    last_heartbeat_at: str
    heartbeat_age_seconds: float | None
    heartbeat_timeout_seconds: float
    assigned_strategy_ids: tuple[str, ...]
    version: str
    git_sha: str
    host_identity: str
    started_at: str
    version_mismatch: bool


@dataclass(frozen=True)
class StrategyHealthView:
    strategy_id: str
    lifecycle_state: str
    runtime_state: str
    account_authorization_state: str | None
    execution_active: bool
    worker_id: str | None
    worker_status: str | None
    account_id: str | None
    broker_id: str | None
    market_data_status: str
    last_market_data_at: str
    last_cycle_at: str
    last_result_summary: str
    last_error: str


@dataclass(frozen=True)
class AccountHealthView:
    account_id: str
    account_name: str
    broker_id: str
    enabled: bool
    authorization_state: str
    execution_mode: str
    assigned_strategy_ids: tuple[str, ...]
    worker_ids: tuple[str, ...]


@dataclass(frozen=True)
class SafetyView:
    kill_switch_engaged: bool
    kill_switch_engaged_by: str
    kill_switch_reason: str
    execution_mode_banner: str
    live_trading_disabled: bool


@dataclass(frozen=True)
class OperationsSnapshot:
    generated_at: str
    system: SystemHealthView
    workers: tuple[WorkerHealthView, ...]
    strategies: tuple[StrategyHealthView, ...]
    accounts: tuple[AccountHealthView, ...]
    portfolio_risk: PortfolioRiskSnapshot
    safety: SafetyView
    active_alerts: tuple[OperationalAlert, ...]


def build_operations_snapshot(
    *,
    strategy_registry: StrategyRegistry,
    strategy_assignment: StrategyAssignment,
    broker_manager: BrokerManager,
    kill_switch: CentralKillSwitch,
    strategy_runtime: StrategyRuntime | None,
    worker_registry: WorkerRegistry | None,
    portfolio_risk_manager: PortfolioRiskManager | None,
    operational_alerts: OperationalAlertStore | None,
    db_ready: bool = True,
) -> OperationsSnapshot:
    deployment = get_deployment_info()
    now = datetime.now(timezone.utc)
    started_at = datetime.fromisoformat(deployment.startup_timestamp)
    uptime_seconds = max(0.0, (now - started_at).total_seconds())

    workers = tuple(_worker_view(w, deployment.git_sha) for w in (worker_registry.list_workers() if worker_registry else []))

    lifecycle_views = check_all_lifecycles(
        strategy_registry=strategy_registry, strategy_assignment=strategy_assignment,
        broker_manager=broker_manager, kill_switch=kill_switch,
    )
    strategies = tuple(
        _strategy_view(v, strategy_runtime, worker_registry) for v in lifecycle_views
    )

    accounts = tuple(_account_view(a, strategy_registry, strategy_assignment, worker_registry) for a in broker_manager.accounts())

    portfolio_risk_snapshot = (
        portfolio_risk_manager.get_snapshot() if portfolio_risk_manager is not None
        else PortfolioRiskSnapshot(
            timestamp=_now_iso(), strategy_id="", strategy_daily_pnl=0.0, strategy_exposure=0.0,
            strategy_open_orders=0, strategy_orders_today=0, account_id="", account_daily_pnl=0.0,
            account_exposure=0.0, account_open_orders=0, account_orders_today=0, portfolio_daily_pnl=0.0,
            portfolio_exposure=0.0, portfolio_open_orders=0, portfolio_orders_today=0,
        )
    )

    execution_modes = {a.execution_mode for a in broker_manager.accounts()}
    execution_mode_banner = (
        "SHADOW" if not execution_modes else
        "LIVE" if any(m.value == "LIVE" for m in execution_modes) else
        "LIVE_CANARY" if any(m.value == "LIVE_CANARY" for m in execution_modes) else
        "SHADOW" if all(m.value in ("SHADOW", "PAPER") for m in execution_modes) else
        "MIXED"
    )

    safety = SafetyView(
        kill_switch_engaged=kill_switch.engaged, kill_switch_engaged_by=kill_switch.engaged_by,
        kill_switch_reason=kill_switch.reason, execution_mode_banner=execution_mode_banner,
        live_trading_disabled=True,
    )

    system = SystemHealthView(
        ready=db_ready, environment=deployment.environment, app_version=deployment.app_version,
        git_sha=deployment.git_sha, deployment_id=deployment.deployment_id,
        started_at=deployment.startup_timestamp, uptime_seconds=uptime_seconds, now=now.isoformat(),
    )

    if operational_alerts is not None:
        _reconcile_alerts(
            alerts=operational_alerts, workers=workers, strategies=strategies, accounts=accounts,
            kill_switch_engaged=kill_switch.engaged, db_ready=db_ready,
        )
    active_alerts = tuple(operational_alerts.active_alerts()) if operational_alerts is not None else ()

    return OperationsSnapshot(
        generated_at=now.isoformat(), system=system, workers=workers, strategies=strategies,
        accounts=accounts, portfolio_risk=portfolio_risk_snapshot, safety=safety, active_alerts=active_alerts,
    )


def _worker_view(w: WorkerInfo, tcc_git_sha: str) -> WorkerHealthView:
    age = None
    if w.last_heartbeat_at:
        try:
            last = datetime.fromisoformat(w.last_heartbeat_at)
            age = (datetime.now(timezone.utc) - last).total_seconds()
        except ValueError:
            age = None
    version_mismatch = bool(w.git_sha) and bool(tcc_git_sha) and tcc_git_sha != "unknown" and w.git_sha != tcc_git_sha
    return WorkerHealthView(
        worker_id=w.worker_id, name=w.name, status=w.status.value, session_id=w.session_id,
        last_heartbeat_at=w.last_heartbeat_at, heartbeat_age_seconds=age,
        heartbeat_timeout_seconds=30.0, assigned_strategy_ids=tuple(w.assigned_strategy_ids),
        version=w.version, git_sha=w.git_sha, host_identity=w.host_identity, started_at=w.started_at,
        version_mismatch=version_mismatch,
    )


def _strategy_view(
    v: StrategyLifecycleView, strategy_runtime: StrategyRuntime | None, worker_registry: WorkerRegistry | None,
) -> StrategyHealthView:
    runtime_status = strategy_runtime.get_status(v.strategy_id) if strategy_runtime else None
    worker_id = worker_registry.get_strategy_owner(v.strategy_id) if worker_registry else None
    worker_status = None
    if worker_id and worker_registry is not None:
        try:
            worker_status = worker_registry.get_worker(worker_id).status.value
        except Exception:  # unknown/removed worker -- never let a stale pointer crash a read
            worker_status = None
    return StrategyHealthView(
        strategy_id=v.strategy_id, lifecycle_state=v.lifecycle_state.value,
        runtime_state=(runtime_status.state.value if runtime_status else RuntimeState.INACTIVE.value),
        account_authorization_state=v.account_authorization_state, execution_active=v.execution_active,
        worker_id=worker_id, worker_status=worker_status, account_id=v.account_id,
        broker_id=None, market_data_status=(runtime_status.market_data_status if runtime_status else ""),
        last_market_data_at=(runtime_status.last_market_data_at if runtime_status else ""),
        last_cycle_at=(runtime_status.last_cycle_at if runtime_status else ""),
        last_result_summary=(runtime_status.last_result_summary if runtime_status else ""),
        last_error=(runtime_status.last_error if runtime_status else v.last_error),
    )


def _account_view(
    account, strategy_registry: StrategyRegistry, strategy_assignment: StrategyAssignment,
    worker_registry: WorkerRegistry | None,
) -> AccountHealthView:
    assigned = tuple(
        s.strategy_id for s in strategy_registry.strategies()
        if strategy_assignment.has_assignment(s.strategy_id) and strategy_assignment.get_account_id(s.strategy_id) == account.account_id
    )
    worker_ids = tuple(sorted({
        wid for sid in assigned
        if worker_registry is not None and (wid := worker_registry.get_strategy_owner(sid)) is not None
    }))
    return AccountHealthView(
        account_id=account.account_id, account_name=account.account_name, broker_id=account.broker_id,
        enabled=account.enabled, authorization_state=account.authorization_state.value,
        execution_mode=account.execution_mode.value, assigned_strategy_ids=assigned, worker_ids=worker_ids,
    )


def _reconcile_alerts(
    *, alerts: OperationalAlertStore, workers: tuple[WorkerHealthView, ...],
    strategies: tuple[StrategyHealthView, ...], accounts: tuple[AccountHealthView, ...],
    kill_switch_engaged: bool, db_ready: bool,
) -> None:
    """The ONE place polling (a GET request) is allowed to write anything:
    updates to the (non-authoritative) OperationalAlertStore only -- see
    module docstring. Every call is idempotent (Section 20): an already-
    active alert for the same condition is never duplicated, and a cleared
    condition resolves its alert exactly once."""
    for w in workers:
        if w.status == WorkerStatus.OFFLINE.value:
            alerts.raise_alert(
                code="WORKER_HEARTBEAT_LOST", severity=AlertSeverity.WARNING, category="WORKER",
                source_type="worker", source_id=w.worker_id,
                message=f"worker {w.worker_id!r} heartbeat exceeded {w.heartbeat_timeout_seconds:.0f}s timeout",
            )
            alerts.raise_alert(
                code="WORKER_OFFLINE", severity=AlertSeverity.CRITICAL, category="WORKER",
                source_type="worker", source_id=w.worker_id, message=f"worker {w.worker_id!r} is OFFLINE",
            )
        else:
            alerts.resolve(code="WORKER_HEARTBEAT_LOST", source_type="worker", source_id=w.worker_id)
            alerts.resolve(code="WORKER_OFFLINE", source_type="worker", source_id=w.worker_id)

        if w.version_mismatch:
            alerts.raise_alert(
                code="WORKER_VERSION_MISMATCH", severity=AlertSeverity.WARNING, category="WORKER",
                source_type="worker", source_id=w.worker_id,
                message=f"worker {w.worker_id!r} git_sha={w.git_sha!r} does not match TCC's own git_sha",
            )
        else:
            alerts.resolve(code="WORKER_VERSION_MISMATCH", source_type="worker", source_id=w.worker_id)

    if kill_switch_engaged:
        alerts.raise_alert(
            code="KILL_SWITCH_ENGAGED", severity=AlertSeverity.CRITICAL, category="SYSTEM",
            source_type="system", source_id="kill_switch", message="central kill switch is engaged",
        )
    else:
        alerts.resolve(code="KILL_SWITCH_ENGAGED", source_type="system", source_id="kill_switch")

    if not db_ready:
        alerts.raise_alert(
            code="SYSTEM_NOT_READY", severity=AlertSeverity.CRITICAL, category="SYSTEM",
            source_type="system", source_id="tcc", message="backend readiness check is failing",
        )
    else:
        alerts.resolve(code="SYSTEM_NOT_READY", source_type="system", source_id="tcc")

    for sv in strategies:
        if sv.runtime_state == RuntimeState.FAILED.value:
            alerts.raise_alert(
                code="STRATEGY_RUNTIME_FAILED", severity=AlertSeverity.CRITICAL, category="STRATEGY",
                source_type="strategy", source_id=sv.strategy_id, message=sv.last_error or "strategy runtime failed",
            )
        else:
            alerts.resolve(code="STRATEGY_RUNTIME_FAILED", source_type="strategy", source_id=sv.strategy_id)

        if sv.market_data_status in _MARKET_DATA_STALE_CODES:
            alerts.raise_alert(
                code="MARKET_DATA_STALE", severity=AlertSeverity.WARNING, category="MARKET_DATA",
                source_type="strategy", source_id=sv.strategy_id, message="required market data is stale",
            )
        else:
            alerts.resolve(code="MARKET_DATA_STALE", source_type="strategy", source_id=sv.strategy_id)

        if sv.market_data_status in _MARKET_DATA_MISSING_CODES:
            alerts.raise_alert(
                code="MARKET_DATA_MISSING", severity=AlertSeverity.WARNING, category="MARKET_DATA",
                source_type="strategy", source_id=sv.strategy_id, message="required market data is missing",
            )
        else:
            alerts.resolve(code="MARKET_DATA_MISSING", source_type="strategy", source_id=sv.strategy_id)

        if sv.market_data_status in _MARKET_DATA_ERROR_CODES:
            alerts.raise_alert(
                code="MARKET_DATA_ERROR", severity=AlertSeverity.CRITICAL, category="MARKET_DATA",
                source_type="strategy", source_id=sv.strategy_id, message=f"market data status={sv.market_data_status}",
            )
        else:
            alerts.resolve(code="MARKET_DATA_ERROR", source_type="strategy", source_id=sv.strategy_id)

    for a in accounts:
        if not a.enabled:
            alerts.raise_alert(
                code="ACCOUNT_DISABLED", severity=AlertSeverity.WARNING, category="ACCOUNT",
                source_type="account", source_id=a.account_id, message=f"account {a.account_id!r} is disabled",
            )
        else:
            alerts.resolve(code="ACCOUNT_DISABLED", source_type="account", source_id=a.account_id)

        if a.authorization_state == "KILLED":
            alerts.raise_alert(
                code="ACCOUNT_KILLED", severity=AlertSeverity.CRITICAL, category="ACCOUNT",
                source_type="account", source_id=a.account_id, message=f"account {a.account_id!r} is KILLED",
            )
        else:
            alerts.resolve(code="ACCOUNT_KILLED", source_type="account", source_id=a.account_id)
