"""
Strategy -- the common interface every trading strategy implements, so
StrategyRegistry (and, later, whatever schedules order generation) never
needs to know which concrete algo it's holding.

    Strategy
    +-- initialize()
    +-- start()
    +-- stop()
    +-- generate_order_intents()
    +-- get_status()
    +-- get_metrics()

Deliberately, and by design, this interface contains NO broker-specific
method or parameter anywhere -- no place_order(), no broker/account
argument, nothing that names a broker. A strategy's only broker-facing
output is generate_order_intents() -> list[OrderIntent], the same
broker-independent OrderIntent type introduced in Phase 1. What happens to
those intents (RiskManager -> ExecutionEngine -> StrategyAssignment ->
TradingAccount -> BrokerManager -> BrokerClient) is entirely outside this
interface's concern, matching the target architecture:

    Strategy -> OrderIntent -> RiskManager -> ExecutionEngine
        -> StrategyAssignment -> TradingAccount -> BrokerManager
        -> BrokerClient -> BrokerAdapter -> Broker API

Phase 10 scope: this module defines the interface, the status vocabulary,
and a concrete BaseStrategy that implements all the bookkeeping (status
transitions, metrics, error handling) so individual strategy adapters only
need to override a few small hooks. It does NOT wire any live algo's real
decision logic into generate_order_intents() -- see
trading/common/strategies/*.py's own module docstrings for why that is
explicitly out of scope for this phase ("do not change trading behavior").
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from trading.common.alerts import AlertManager
from trading.common.observability import (
    EVENT_STRATEGY_STARTED,
    EVENT_STRATEGY_STOPPED,
    AuditTrail,
    MetricsRegistry,
)
from trading.common.order_intent import OrderIntent
from trading.common.trading_account import ExecutionMode


class StrategyStatus(str, Enum):
    """The full lifecycle vocabulary a strategy can be in. "shadow" is
    deliberately distinct from "running": a strategy started with
    execution_mode=SHADOW reports SHADOW, not RUNNING, so a caller can
    always tell whether a given "the strategy is active" state is
    generating real-flow-eligible intents or shadow-only ones without
    having to separately consult execution_mode."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"
    SHADOW = "shadow"


_ACTIVE_STATUSES = frozenset({StrategyStatus.RUNNING, StrategyStatus.SHADOW})


class InvalidStrategyStateError(ValueError):
    """Raised when a lifecycle method is called from a status that doesn't
    permit it (e.g. start() while DISABLED, or enable() while RUNNING)."""


@dataclass
class StrategyMetrics:
    """Read-only snapshot returned by get_metrics(). Deliberately generic
    (no broker/order-outcome fields) -- a strategy only ever knows how many
    intents IT generated, not what happened to them downstream."""

    strategy_id: str
    status: StrategyStatus
    intents_generated: int = 0
    error_count: int = 0
    last_error: str = ""
    started_at: str = ""
    stopped_at: str = ""
    last_intent_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Strategy(ABC):
    """Common interface every strategy adapter implements. See the module
    docstring -- no broker-specific method belongs here, ever."""

    strategy_id: str

    @abstractmethod
    def initialize(self) -> None:
        """One-time setup (config validation, internal state reset). Must
        not connect to a broker or place any order."""

    @abstractmethod
    def start(self) -> None:
        """Transition into an active (RUNNING or SHADOW) state."""

    @abstractmethod
    def stop(self) -> None:
        """Transition out of an active state into STOPPED."""

    @abstractmethod
    def generate_order_intents(self) -> list[OrderIntent]:
        """Return zero or more new OrderIntent objects. Must never itself
        call a broker -- that is RiskManager/ExecutionEngine's job, entirely
        outside this interface."""

    @abstractmethod
    def get_status(self) -> StrategyStatus:
        ...

    @abstractmethod
    def get_metrics(self) -> StrategyMetrics:
        ...


class BaseStrategy(Strategy):
    """Concrete lifecycle/metrics bookkeeping shared by every strategy
    adapter. Subclasses (see trading/common/strategies/*.py) override only
    the small `_on_*` hooks below -- they never need to reimplement status
    transitions or error handling.

    execution_mode defaults to SHADOW: every concrete strategy adapter
    built in this phase is shadow-only by construction (see the module
    docstring's Phase 10 scope note) -- matching this repo's established
    convention (Phase 3's execution_bridge, Phase 5B's
    ConnectedShadowBroker) of defaulting new integration surfaces to the
    safest mode rather than to LIVE.
    """

    def __init__(
        self,
        strategy_id: str,
        *,
        execution_mode: ExecutionMode | str = ExecutionMode.SHADOW,
        metrics_registry: MetricsRegistry | None = None,
        audit_trail: AuditTrail | None = None,
        alerts: AlertManager | None = None,
    ) -> None:
        self.strategy_id = strategy_id
        self._execution_mode = ExecutionMode(execution_mode)
        self._status = StrategyStatus.DISABLED
        self._metrics = StrategyMetrics(strategy_id=strategy_id, status=self._status)
        # Phase 13 observability -- all optional, all default None. Named
        # `metrics_registry` (not `metrics`) to avoid colliding with
        # self._metrics above (this class's own, pre-existing Phase 10
        # StrategyMetrics snapshot -- a different, older concept).
        self._metrics_registry = metrics_registry
        self._audit_trail = audit_trail
        self._alerts = alerts

    def __repr__(self) -> str:
        return f"{type(self).__name__}(strategy_id={self.strategy_id!r}, status={self._status.value})"

    @property
    def execution_mode(self) -> ExecutionMode:
        return self._execution_mode

    # -- overridable hooks for concrete strategies ---------------------------- #
    def _on_initialize(self) -> None:
        """Subclass hook: default no-op."""

    def _on_start(self) -> None:
        """Subclass hook: default no-op."""

    def _on_stop(self) -> None:
        """Subclass hook: default no-op."""

    def _on_generate_order_intents(self) -> list[OrderIntent]:
        """Subclass hook: default returns no intents. See each concrete
        strategy adapter's module docstring for why real decision-logic
        integration is deferred past this phase."""
        return []

    # -- Strategy interface ---------------------------------------------------- #
    def initialize(self) -> None:
        self._on_initialize()
        if self._status == StrategyStatus.DISABLED:
            return  # already the correct post-initialize state
        self._status = StrategyStatus.DISABLED

    def enable(self) -> None:
        if self._status not in (StrategyStatus.DISABLED, StrategyStatus.STOPPED, StrategyStatus.ERROR):
            raise InvalidStrategyStateError(
                f"Cannot enable '{self.strategy_id}' from status {self._status.value!r}."
            )
        self._status = StrategyStatus.ENABLED

    def disable(self) -> None:
        if self._status in _ACTIVE_STATUSES or self._status == StrategyStatus.STARTING:
            raise InvalidStrategyStateError(
                f"Cannot disable '{self.strategy_id}' while status is {self._status.value!r}; stop() it first."
            )
        self._status = StrategyStatus.DISABLED

    def start(self) -> None:
        if self._status != StrategyStatus.ENABLED:
            raise InvalidStrategyStateError(
                f"Cannot start '{self.strategy_id}' from status {self._status.value!r}; enable() it first."
            )
        self._status = StrategyStatus.STARTING
        try:
            self._on_start()
        except Exception as exc:
            self._mark_error(exc)
            raise
        self._status = StrategyStatus.SHADOW if self._execution_mode == ExecutionMode.SHADOW else StrategyStatus.RUNNING
        self._metrics.started_at = _now()
        if self._metrics_registry is not None:
            self._metrics_registry.record_strategy_heartbeat(self.strategy_id)
        if self._audit_trail is not None:
            self._audit_trail.append(
                EVENT_STRATEGY_STARTED, strategy_id=self.strategy_id, execution_mode=self._execution_mode.value,
            )

    def stop(self) -> None:
        if self._status not in _ACTIVE_STATUSES:
            raise InvalidStrategyStateError(
                f"Cannot stop '{self.strategy_id}' from status {self._status.value!r}; it is not active."
            )
        try:
            self._on_stop()
        except Exception as exc:
            self._mark_error(exc)
            raise
        self._status = StrategyStatus.STOPPED
        self._metrics.stopped_at = _now()
        if self._audit_trail is not None:
            self._audit_trail.append(EVENT_STRATEGY_STOPPED, strategy_id=self.strategy_id)
        if self._alerts is not None:
            self._alerts.strategy_stopped(self.strategy_id)

    def generate_order_intents(self) -> list[OrderIntent]:
        if self._status not in _ACTIVE_STATUSES:
            raise InvalidStrategyStateError(
                f"Cannot generate order intents for '{self.strategy_id}' from status {self._status.value!r}; "
                "it must be RUNNING or SHADOW."
            )
        if self._metrics_registry is not None:
            self._metrics_registry.record_strategy_heartbeat(self.strategy_id)
        try:
            intents = self._on_generate_order_intents()
        except Exception as exc:
            self._mark_error(exc)
            raise
        if intents:
            self._metrics.intents_generated += len(intents)
            self._metrics.last_intent_at = _now()
        return intents

    def get_status(self) -> StrategyStatus:
        return self._status

    def get_metrics(self) -> StrategyMetrics:
        self._metrics.status = self._status
        return self._metrics

    def mark_error(self, reason: str) -> None:
        """Externally-triggerable transition into ERROR (e.g. a supervisor
        detecting a fatal condition outside this strategy's own control).
        Not part of the Strategy ABC -- an optional registry-facing extra,
        same spirit as BrokerClient's duck-typed get_order/modify_order."""
        self._mark_error(RuntimeError(reason))

    def _mark_error(self, exc: Exception) -> None:
        self._status = StrategyStatus.ERROR
        self._metrics.error_count += 1
        self._metrics.last_error = str(exc)
        if self._metrics_registry is not None:
            self._metrics_registry.record_error(f"strategy:{self.strategy_id}")
