"""
Phase 15D.9 -- read-only pre-canary operational check reporting whether
each of the three legacy (pre-Phase-15D) algo processes is in a state
safe to have running during a live-canary window.

This module NEVER mutates a process -- there is no stop/kill/start
capability anywhere in it, only a SELECT against the existing
`algos` table (`trading/database/models.py::Algo`, already populated by
the existing control-center start/stop/restart routes). It is a
reporting/gating SIGNAL, consulted by a human (or a future 15D.10
runbook step) before authorizing anything -- consistent with this
project's standing rule that no automated code path may make a
live-trading decision.

Conservative by design: this schema does not currently track a running
algo process's own `BOT_DRY_RUN` env var anywhere, so a non-stopped algo
can never be positively classified DRY_RUN from the database alone --
absent a `known_dry_run` override supplied by a caller that has some
other, independent way to confirm it (e.g. a future enhancement reading
a state file the agent writes), a running algo is always reported
RUNNING, never assumed safe.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from trading.database import models

LEGACY_ALGO_NAMES = ("DoubleStraddelAlgo", "CombinedVwapNifty", "Vwap_Algo_Nifty_hedge")

_STOPPED_STATUS_VALUES = frozenset({"", "STOPPED", "NEVER_STARTED"})


class LegacyAlgoState:
    STOPPED = "STOPPED"
    DRY_RUN = "DRY_RUN"
    RUNNING = "RUNNING"
    UNKNOWN = "UNKNOWN"


_ACCEPTABLE_FOR_CANARY = frozenset({LegacyAlgoState.STOPPED, LegacyAlgoState.DRY_RUN})


@dataclass(frozen=True)
class LegacyAlgoReadiness:
    name: str
    state: str
    detail: str

    @property
    def acceptable_for_canary(self) -> bool:
        return self.state in _ACCEPTABLE_FOR_CANARY


def check_legacy_algo_state(
    db: Session, name: str, *, known_dry_run: bool | None = None,
) -> LegacyAlgoReadiness:
    """Read-only: one SELECT against the existing `algos` table. Never
    raises to the caller for an ordinary query outcome -- a query
    failure or a missing row is reported as UNKNOWN, never silently
    treated as safe.

    `known_dry_run`: optional, supplied only by a caller that has an
    independent, trustworthy way to confirm the running process's own
    dry-run state (this schema doesn't track it). When True and the algo
    is not STOPPED, reports DRY_RUN instead of RUNNING. Defaults to None
    (unknown), which never upgrades a running algo to DRY_RUN by
    assumption."""
    try:
        algo = db.query(models.Algo).filter(models.Algo.name == name).one_or_none()
    except Exception as exc:  # noqa: BLE001 -- a query failure must never be silently treated as safe
        return LegacyAlgoReadiness(name=name, state=LegacyAlgoState.UNKNOWN, detail=f"query failed: {type(exc).__name__}")

    if algo is None:
        return LegacyAlgoReadiness(name=name, state=LegacyAlgoState.UNKNOWN, detail="no registered algo row found")

    status = (algo.status or "").strip().upper()
    if status in _STOPPED_STATUS_VALUES:
        return LegacyAlgoReadiness(name=name, state=LegacyAlgoState.STOPPED, detail=f"algo status={algo.status!r}")

    if known_dry_run is True:
        return LegacyAlgoReadiness(
            name=name, state=LegacyAlgoState.DRY_RUN,
            detail=f"algo status={algo.status!r}, confirmed dry-run by caller",
        )

    return LegacyAlgoReadiness(
        name=name, state=LegacyAlgoState.RUNNING,
        detail=f"algo status={algo.status!r} (dry-run state not tracked in this schema -- reported conservatively)",
    )


def check_all_legacy_algos(db: Session) -> list[LegacyAlgoReadiness]:
    return [check_legacy_algo_state(db, name) for name in LEGACY_ALGO_NAMES]


def all_acceptable_for_canary(results: list[LegacyAlgoReadiness]) -> bool:
    return all(r.acceptable_for_canary for r in results)
