"""
Phase 15D.9 -- closes Blocker 1 (legacy algo broker bypass) with the
smallest safe fix: reuses the EXISTING, unmodified `CentralKillSwitch`
(never a second implementation of it) as a read-only, fail-safe-by-default
check that the three legacy, pre-Phase-15D algo scripts
(`trading/algos/DoubleStraddelAlgo/broker/orders.py`,
`trading/algos/CombinedVwapNifty/rest_func.py`,
`trading/algos/Vwap_Algo_Nifty_hedge/rest_func.py`) call immediately
before their own direct `config.objconn.placeOrder(...)` call.

What this DOES close: the central kill switch (the one control this
project has built the most safety infrastructure around) now reaches
all four order-placement paths in this repository, not just the new
`StrategyExecutionEngine` one -- engaging it now blocks these legacy
scripts too.

What this does NOT close, and does not attempt to: RiskManager,
LiveAuthorization, and idempotency are NOT wired into these scripts.
Doing so would require either (a) a second, duplicate implementation of
those controls inside three standalone legacy processes -- explicitly
forbidden by this phase's own brief ("do not duplicate RiskManager/
authorization logic inside each strategy") -- or (b) migrating these
algos onto the centralized `StrategyExecutionEngine` entirely, a
multi-phase architectural project, not a hardening fix. This is a named,
documented, accepted limitation -- see
docs/phase-15d-9-final-readiness-report.md.

Fail-safe design: if `KILL_SWITCH_PERSISTENCE_PATH` is not set in this
process's own environment, this module preserves the EXACT pre-existing
default (`CentralKillSwitch(persistence_path=None)` starts disengaged,
in-memory-only -- the same default every other caller of
`CentralKillSwitch` in this codebase already gets) rather than inventing
a new, stricter default that could make an already-running legacy
process fail in an untested way. Configuring
`KILL_SWITCH_PERSISTENCE_PATH` for these legacy processes' own
environment (so they see the SAME persisted, shared switch state the
FastAPI control-center process does) is an operational/deployment step,
not something this module can force from inside a library import.
"""
from __future__ import annotations

import os

from trading.common.kill_switch import CentralKillSwitch


class LegacyExecutionBlocked(RuntimeError):
    """Raised by `assert_live_mutation_allowed()` when the shared,
    persisted CentralKillSwitch is engaged. Callers (the three legacy
    algo scripts) are expected to let this propagate and abandon the
    order attempt -- never to catch it and retry."""


def assert_live_mutation_allowed(*, strategy_id: str) -> None:
    """Call this immediately before a real `placeOrder`/broker-mutation
    call in legacy (pre-Phase-15D) algo code. Reads
    `KILL_SWITCH_PERSISTENCE_PATH` from the CURRENT process environment
    fresh on every call (no caching) -- a kill switch engaged by an
    operator via the control-center API takes effect on this legacy
    process's very next order attempt, without requiring a restart,
    exactly like it already does for `StrategyExecutionEngine`.

    Never mutates anything itself: constructing `CentralKillSwitch` with
    a persistence path only READS that file (see kill_switch.py's own
    `_load()`); it never writes unless `.engage()`/`.disengage()` is
    called, which this function never does.
    """
    path = os.environ.get("KILL_SWITCH_PERSISTENCE_PATH", "").strip()
    switch = CentralKillSwitch(persistence_path=path or None)
    if switch.engaged:
        raise LegacyExecutionBlocked(
            f"central kill switch is engaged (reason={switch.reason!r}, "
            f"by={switch.engaged_by!r}) -- refusing to place a real broker "
            f"order for strategy {strategy_id!r}"
        )
