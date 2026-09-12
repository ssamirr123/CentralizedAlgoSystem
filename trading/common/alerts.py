"""
Phase 13 -- AlertManager: the 7 named alert types this phase's brief asks
for, each a thin, self-documenting method over a shared _raise() core.

Alerts are observational only -- raising one never blocks, retries, or
otherwise affects the action that triggered it (an execution_error alert
does not stop or retry the execution; a kill_switch alert does not itself
flip anything). This mirrors trading/api/security/audit.py's own "auditing
must never break the request" discipline, extended to alerting.

An AlertManager may optionally be constructed with an AuditTrail -- every
alert raised is also appended there (as "ALERT_<TYPE>"), so an alert is
never a dead end: it's traceable by correlation_id alongside the rest of
that order's lifecycle, and it survives in the tamper-evident audit trail
even after AlertManager's own bounded in-memory deque has rotated it out.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from trading.common.observability import AuditTrail

_log = logging.getLogger("trading.alerts")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

ALERT_STRATEGY_STOPPED = "STRATEGY_STOPPED"
ALERT_BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
ALERT_RISK_BREACH = "RISK_BREACH"
ALERT_DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
ALERT_UNEXPECTED_POSITION = "UNEXPECTED_POSITION"
ALERT_EXECUTION_ERROR = "EXECUTION_ERROR"
ALERT_KILL_SWITCH = "KILL_SWITCH"
ALERT_EMERGENCY_SHUTDOWN = "EMERGENCY_SHUTDOWN"

_SEVERITY_INFO = "INFO"
_SEVERITY_WARNING = "WARNING"
_SEVERITY_CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Alert:
    seq: int
    timestamp: str
    alert_type: str
    severity: str
    message: str
    correlation_id: str
    detail: dict[str, Any]


class AlertManager:
    def __init__(self, *, audit_trail: AuditTrail | None = None, max_alerts: int = 1000) -> None:
        self._lock = threading.Lock()
        self._alerts: deque[Alert] = deque(maxlen=max_alerts)
        self._seq = 0
        self._audit_trail = audit_trail

    def _raise(self, alert_type: str, severity: str, message: str, *, correlation_id: str = "", **detail: Any) -> Alert:
        with self._lock:
            seq = self._seq
            self._seq += 1
            alert = Alert(seq, _now(), alert_type, severity, message, correlation_id, dict(detail))
            self._alerts.append(alert)
        log_fn = _log.critical if severity == _SEVERITY_CRITICAL else _log.warning if severity == _SEVERITY_WARNING else _log.info
        log_fn("[ALERT] %s", {"alert_type": alert_type, "severity": severity, "message": message, "correlation_id": correlation_id, **detail})
        if self._audit_trail is not None:
            self._audit_trail.append(f"ALERT_{alert_type}", correlation_id=correlation_id, severity=severity, message=message, **detail)
        return alert

    def alerts(self) -> list[Alert]:
        with self._lock:
            return list(self._alerts)

    # -- named alert triggers, one per requested type ------------------------- #
    def strategy_stopped(self, strategy_id: str, *, reason: str = "") -> Alert:
        return self._raise(
            ALERT_STRATEGY_STOPPED, _SEVERITY_WARNING, f"Strategy '{strategy_id}' stopped",
            strategy_id=strategy_id, reason=reason,
        )

    def broker_disconnected(self, broker_id: str, *, reason: str = "") -> Alert:
        return self._raise(
            ALERT_BROKER_DISCONNECTED, _SEVERITY_CRITICAL, f"Broker '{broker_id}' disconnected",
            broker_id=broker_id, reason=reason,
        )

    def risk_breach(self, strategy_id: str, check_name: str, reason: str, *, correlation_id: str = "") -> Alert:
        return self._raise(
            ALERT_RISK_BREACH, _SEVERITY_WARNING, f"Risk check '{check_name}' failed for '{strategy_id}': {reason}",
            correlation_id=correlation_id, strategy_id=strategy_id, check_name=check_name, reason=reason,
        )

    def daily_loss_limit(self, account_id: str, pnl: float, limit: float, *, strategy_id: str = "") -> Alert:
        return self._raise(
            ALERT_DAILY_LOSS_LIMIT, _SEVERITY_CRITICAL,
            f"Account '{account_id}' daily P&L {pnl:.2f} breached limit {limit:.2f}",
            strategy_id=strategy_id, account_id=account_id, pnl=pnl, limit=limit,
        )

    def unexpected_position(self, strategy_id: str, symbol: str, quantity: int, *, expected: int | None = None) -> Alert:
        return self._raise(
            ALERT_UNEXPECTED_POSITION, _SEVERITY_WARNING,
            f"Unexpected position for '{strategy_id}': {symbol}={quantity}" + (f" (expected {expected})" if expected is not None else ""),
            strategy_id=strategy_id, symbol=symbol, quantity=quantity, expected=expected,
        )

    def execution_error(self, strategy_id: str, error: str, *, correlation_id: str = "") -> Alert:
        return self._raise(
            ALERT_EXECUTION_ERROR, _SEVERITY_CRITICAL, f"Execution error for '{strategy_id}': {error}",
            correlation_id=correlation_id, strategy_id=strategy_id, error=error,
        )

    def kill_switch(self, engaged: bool, *, by: str = "", reason: str = "") -> Alert:
        return self._raise(
            ALERT_KILL_SWITCH, _SEVERITY_CRITICAL if engaged else _SEVERITY_INFO,
            f"Kill switch {'engaged' if engaged else 'disengaged'}" + (f" by {by}" if by else ""),
            by=by, reason=reason, engaged=engaged,
        )

    def emergency_shutdown(self, reason: str, *, by: str = "") -> Alert:
        """Phase 14 -- LIVE_CANARY's emergency shutdown. Always CRITICAL;
        distinct from kill_switch() because an emergency shutdown is
        irreversible for the LiveCanaryGuard instance that raised it (see
        trading/common/live_canary.py), whereas a kill switch can be
        disengaged."""
        return self._raise(
            ALERT_EMERGENCY_SHUTDOWN, _SEVERITY_CRITICAL, f"Emergency shutdown: {reason}",
            by=by, reason=reason,
        )
