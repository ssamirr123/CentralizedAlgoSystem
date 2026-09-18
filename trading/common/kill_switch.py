"""
Phase 14.6 -- CentralKillSwitch: one authoritative execution-authorization
gate, replacing the two independent, disconnected kill switches Phase
14.5's review found (Phase 11's API-level ExecutionState.kill_switch, and
Phase 14's per-LiveCanaryGuard kill switch).

    REMOTE KILL SWITCH
        -> central execution authorization state (THIS CLASS)
        -> ALL future live/canary order authorization fails

StrategyExecutionEngine.execute() checks this FIRST, before ANY other
logic (idempotency replay, risk validation, canary authorization,
account/broker resolution) -- engaging it blocks every execution_mode
alike (LIVE, LIVE_CANARY, PAPER, SHADOW), because "no new orders" must
mean exactly that, unconditionally.

This does NOT replace or weaken LiveCanaryGuard's own kill switch (see
trading/common/live_canary.py) -- that remains fully in place, checked
independently inside authorize(), as defense in depth: a caller that
somehow constructs a StrategyExecutionEngine without a central_kill_switch
attached is still protected by the canary guard's own switch for
LIVE_CANARY accounts specifically.

Public attribute names (engaged/engaged_by/reason/engaged_at/
disengaged_at) and engage()/disengage() signatures deliberately match
trading/api/execution_state.py's PRE-Phase-14.6 KillSwitch dataclass
exactly, so that module (and every existing route/test built against it)
needed zero changes beyond swapping which class is constructed.

Phase 15D-DR update: this object was previously in-memory only, meaning a
process restart always reset it to disengaged -- safe when it was never
engaged, but dangerous the one time it matters most: an operator engages
it for a real reason, the process restarts (deploy, crash, orchestrator
reschedule), and the switch silently comes back disengaged with no signal
that anything changed. `persistence_path`, when given, closes this gap:
engage()/disengage() write the new state to a small local file (mirroring
trading.common.idempotency_store.py's own restart-safety pattern, without
needing a database) before returning, and __init__ loads any prior state
from that file. Passing no `persistence_path` (the default) preserves the
exact original in-memory-only behavior byte-for-byte -- no existing caller
needs to change.

If the persistence file exists but cannot be read (corrupt, permissions),
this fails CLOSED in the safest available direction: the switch loads as
ENGAGED with a reason noting the read failure, rather than silently
falling back to disengaged and potentially believing a real kill-switch
event never happened. An operator must explicitly disengage it once they
have confirmed the real intended state.

Phase 15D-AUDIT update: an optional `audit_trail` may also be given so
every engage()/disengage() is durably recorded (Area J: "every kill-switch
state change must be auditable"), independent of `persistence_path` (a
caller may use either, both, or neither). The audit call is intentionally
best-effort: engaging/disengaging the switch itself must NEVER fail or be
delayed because an audit write failed -- inverting that priority would
mean a real "stop trading now" action could be blocked by an audit hiccup,
exactly the kind of dangerous coupling Phase 14.6 Blocker C already
rejected for the execution path. A failed audit write here is logged, not
raised or retried; the switch's own persisted state (when
`persistence_path` is set) remains the authoritative record of whether it
is engaged.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading.common.file_permissions import harden_file_permissions

_log = logging.getLogger("trading.kill_switch")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CentralKillSwitch:
    def __init__(
        self, *, persistence_path: str | Path | None = None, audit_trail: Any | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._persistence_path = Path(persistence_path) if persistence_path else None
        self._audit_trail = audit_trail
        self.engaged: bool = False
        self.engaged_by: str = ""
        self.reason: str = ""
        self.engaged_at: str = ""
        self.disengaged_at: str = ""
        if self._persistence_path is not None:
            self._load()

    def _audit(self, event_type: str, *, previous_engaged: bool, new_engaged: bool, actor: str, reason: str) -> None:
        if self._audit_trail is None:
            return
        try:
            self._audit_trail.append(
                event_type, correlation_id="", strategy_id="",
                previous_engaged=previous_engaged, new_engaged=new_engaged, actor=actor, reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 -- see module docstring: never let audit block the switch
            _log.error("[KILL SWITCH AUDIT FAILURE] event=%s error=%s -- switch state itself is UNAFFECTED", event_type, exc)

    def _load(self) -> None:
        if not self._persistence_path.is_file():
            return  # no prior state -- safe default (disengaged) stands
        try:
            data = json.loads(self._persistence_path.read_text(encoding="utf-8"))
            self.engaged = bool(data["engaged"])
            self.engaged_by = str(data.get("engaged_by", ""))
            self.reason = str(data.get("reason", ""))
            self.engaged_at = str(data.get("engaged_at", ""))
            self.disengaged_at = str(data.get("disengaged_at", ""))
        except Exception as exc:  # noqa: BLE001 -- any read/parse failure fails closed
            # Fail CLOSED: an unreadable persistence file must never be
            # silently treated as "disengaged". See module docstring.
            self.engaged = True
            self.engaged_by = "system"
            self.reason = f"kill switch persistence file unreadable at startup ({type(exc).__name__}); failing closed"
            self.engaged_at = _now()

    def _save(self) -> None:
        if self._persistence_path is None:
            return
        payload = {
            "engaged": self.engaged, "engaged_by": self.engaged_by, "reason": self.reason,
            "engaged_at": self.engaged_at, "disengaged_at": self.disengaged_at,
        }
        self._persistence_path.write_text(json.dumps(payload), encoding="utf-8")
        harden_file_permissions(str(self._persistence_path))

    def engage(self, *, by: str = "", reason: str = "") -> None:
        with self._lock:
            previous = self.engaged
            self.engaged = True
            self.engaged_by = by
            self.reason = reason
            self.engaged_at = _now()
            self._save()
        self._audit("KILL_SWITCH_ENGAGED", previous_engaged=previous, new_engaged=True, actor=by, reason=reason)

    def disengage(self, *, by: str = "") -> None:
        """Disengaging removes ONLY this gate's own block -- it never
        grants, restores, or bypasses any other authorization (RiskManager,
        LiveCanaryGuard, execution_mode/RiskLimits requirements). Those are
        independent gates, checked separately and unconditionally by
        StrategyExecutionEngine.execute() regardless of this switch's state."""
        with self._lock:
            previous = self.engaged
            self.engaged = False
            self.engaged_by = by
            self.reason = ""
            self.disengaged_at = _now()
            self._save()
        self._audit("KILL_SWITCH_DISENGAGED", previous_engaged=previous, new_engaged=False, actor=by, reason="")
