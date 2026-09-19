"""
OperationalAlertStore -- Phase 16.11: a small, centralized, PRESENTATION-
ONLY alert model for the TCC operations dashboard.

This is deliberately separate from `trading.common.alerts.AlertManager`
(Phase 13), which already covers a fixed, narrow set of named alert types
(strategy_stopped/broker_disconnected/risk_breach/...) with no active/
resolved concept at all -- AlertManager.alerts() is an append-only log, not
a "what's currently wrong" view. This module adds exactly the missing
piece: CURRENT, deduplicated, resolvable alert state, without replacing or
duplicating AlertManager's own responsibilities.

    detect -> deduplicate -> display -> resolve -> audit

An alert's identity is (code, source_type, source_id) -- e.g.
("WORKER_OFFLINE", "worker", "worker-cvn"). raise_alert() is idempotent:
calling it again for an already-active identity is a no-op (never a
duplicate row, never a bumped timestamp) -- this is what prevents "Worker
offline #1/#2/#3..." spam under repeated polling (Section 20). resolve()
is likewise a no-op if nothing is currently active for that identity.

THIS IS A CLASSIFICATION LAYER, NOT A SECOND RISK ENGINE: nothing in this
module ever evaluates a trading decision, touches RiskLimits/
PortfolioRiskLimits, or blocks/allows an OrderIntent. It only records that
some OTHER, authoritative component (WorkerRegistry, PortfolioRiskManager,
CentralKillSwitch, StrategyRuntime, ...) already observed a fact.

PERSISTENCE: the active/resolved alert list itself is in-memory only, for
this phase (matches every other ExecutionState collaborator's default --
kill_switch, metrics, worker_registry -- none of which persist across a
restart unless explicitly configured). When an `audit_trail` is supplied
(optionally a Phase 15D-AUDIT PersistentAuditTrail, i.e. SQLite-backed),
every raise/resolve IS durably recorded there -- so the HISTORY of an
alert's lifecycle can survive a restart even though the in-memory "is it
currently active" list resets. This reuses the repository's existing
persistence mechanism rather than inventing a new one (see this module's
own report section on alert persistence for the honest limitation this
implies: a worker that goes offline, and back online, entirely between
two process restarts would not have its resolution durably recorded
unless the audit_trail itself is the persistent kind).
"""
from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

__all__ = [
    "AlertSeverity",
    "OperationalAlert",
    "OperationalAlertStore",
]

_MAX_HISTORY = 500


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class OperationalAlert:
    alert_id: str
    code: str
    severity: AlertSeverity
    category: str
    source_type: str
    source_id: str
    message: str
    raised_at: str
    active: bool
    resolved_at: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OperationalAlertStore:
    def __init__(self, *, audit_trail: Any | None = None, clock=None) -> None:
        self._audit_trail = audit_trail
        self._clock = clock or _now
        self._lock = threading.Lock()
        # Keyed by (code, source_type, source_id) -- only ever holds the
        # CURRENT alert (active or last-resolved) for that identity.
        self._current: dict[tuple[str, str, str], OperationalAlert] = {}
        # Bounded history, newest first, capped so this never grows
        # unbounded across a long-running process (Section 40).
        self._history: deque[OperationalAlert] = deque(maxlen=_MAX_HISTORY)

    def raise_alert(
        self, *, code: str, severity: AlertSeverity, category: str,
        source_type: str, source_id: str, message: str,
    ) -> OperationalAlert:
        key = (code, source_type, source_id)
        with self._lock:
            existing = self._current.get(key)
            if existing is not None and existing.active:
                return existing  # idempotent -- no duplicate, no timestamp bump

            alert = OperationalAlert(
                alert_id=str(uuid.uuid4()), code=code, severity=severity, category=category,
                source_type=source_type, source_id=source_id, message=message,
                raised_at=self._clock(), active=True, resolved_at=None,
            )
            self._current[key] = alert
            self._history.appendleft(alert)

        if self._audit_trail is not None:
            self._audit_trail.append(
                f"ALERT_RAISED_{code}", strategy_id=(source_id if source_type == "strategy" else ""),
                alert_id=alert.alert_id, severity=severity.value, category=category,
                source_type=source_type, source_id=source_id, message=message,
            )
        return alert

    def resolve(self, *, code: str, source_type: str, source_id: str) -> OperationalAlert | None:
        key = (code, source_type, source_id)
        with self._lock:
            existing = self._current.get(key)
            if existing is None or not existing.active:
                return None
            resolved = OperationalAlert(
                alert_id=existing.alert_id, code=existing.code, severity=existing.severity,
                category=existing.category, source_type=existing.source_type, source_id=existing.source_id,
                message=existing.message, raised_at=existing.raised_at, active=False, resolved_at=self._clock(),
            )
            self._current[key] = resolved
            self._history.appendleft(resolved)

        if self._audit_trail is not None:
            self._audit_trail.append(
                f"ALERT_RESOLVED_{code}", strategy_id=(source_id if source_type == "strategy" else ""),
                alert_id=resolved.alert_id, source_type=source_type, source_id=source_id,
            )
        return resolved

    def active_alerts(self) -> list[OperationalAlert]:
        with self._lock:
            return [a for a in self._current.values() if a.active]

    def all_alerts(self, *, limit: int = 200) -> list[OperationalAlert]:
        """Bounded, newest-first -- includes resolved alerts (a lightweight
        history view), never the unbounded full lifetime of the process."""
        with self._lock:
            return list(self._history)[: max(0, limit)]
