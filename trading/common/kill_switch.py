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

Known, documented limitation: this object is in-memory only, exactly
like every other field on Phase 11's ExecutionState (which itself has no
persistence for anything). A process restart resets it to disengaged
(the SAFE default) -- an operator who intended it to remain engaged must
re-engage it after a restart. This is a deliberate, safer-by-default
choice over a kill switch that could silently fail to persist while
being believed engaged; a future phase adding the same durable-storage
treatment as trading.common.idempotency_store.py would close this gap
without changing this class's interface.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CentralKillSwitch:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.engaged: bool = False
        self.engaged_by: str = ""
        self.reason: str = ""
        self.engaged_at: str = ""
        self.disengaged_at: str = ""

    def engage(self, *, by: str = "", reason: str = "") -> None:
        with self._lock:
            self.engaged = True
            self.engaged_by = by
            self.reason = reason
            self.engaged_at = _now()

    def disengage(self, *, by: str = "") -> None:
        """Disengaging removes ONLY this gate's own block -- it never
        grants, restores, or bypasses any other authorization (RiskManager,
        LiveCanaryGuard, execution_mode/RiskLimits requirements). Those are
        independent gates, checked separately and unconditionally by
        StrategyExecutionEngine.execute() regardless of this switch's state."""
        with self._lock:
            self.engaged = False
            self.engaged_by = by
            self.reason = ""
            self.disengaged_at = _now()
