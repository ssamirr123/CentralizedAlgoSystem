"""
Phase 15D.8 -- a read-only clock-drift diagnostic.

Compares this process's local clock against an optional configured
reference time source and reports the drift as a plain fact for a human
or dashboard to look at. It NEVER changes trading behavior, never blocks
startup, never engages the kill switch, and never itself decides that
drift is "too much" to trade on -- consistent with this project's
standing precedent (established across every Phase 15D sub-phase) that
no automated code path may make a live-trading decision; a human always
does. This module produces one more fact for that human, nothing more.

No reference source is configured by default (`TIME_SYNC_REFERENCE_URL`
unset) -- in that case `check_clock_drift()` returns a `not_checked`
result immediately, at zero cost, so every existing deployment and every
test sees no behavior change until an operator opts in.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

# A reference clock is anything that returns the current UTC time as a
# `datetime`. In production this would be wired to an NTP query or a
# broker/exchange server-time endpoint (read-only, no order API) -- no
# such wiring exists yet (this phase only builds the diagnostic and its
# reporting surface, see docs/phase-15d-8-*-report.md's "known
# limitations"). Tests inject a fake reference directly.
ReferenceClock = Callable[[], datetime]

DEFAULT_WARN_THRESHOLD_SECONDS = 2.0
DEFAULT_CHECK_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class ClockDriftResult:
    status: str  # "not_checked" | "in_sync" | "drift_detected" | "check_failed"
    drift_seconds: float | None  # None when not_checked or check_failed
    threshold_seconds: float
    detail: str
    checked_at: str


def _not_checked(threshold: float, detail: str = "no reference clock configured") -> ClockDriftResult:
    return ClockDriftResult(
        status="not_checked", drift_seconds=None, threshold_seconds=threshold, detail=detail,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )


def check_clock_drift(
    *, reference_clock: ReferenceClock | None = None, warn_threshold_seconds: float = DEFAULT_WARN_THRESHOLD_SECONDS,
    timeout_seconds: float = DEFAULT_CHECK_TIMEOUT_SECONDS,
) -> ClockDriftResult:
    """Read-only. Never raises -- any failure to reach/parse a reference
    clock is reported as `check_failed`, not propagated. `reference_clock`
    is normally None in production today (no reference source is wired
    yet -- see module docstring), which returns `not_checked` immediately.
    A future caller may pass one (e.g., an NTP query or broker server-time
    call) without any change to this function's contract."""
    if reference_clock is None:
        return _not_checked(warn_threshold_seconds)

    start = time.monotonic()
    try:
        reference_now = reference_clock()
        elapsed = time.monotonic() - start
        if elapsed > timeout_seconds:
            return ClockDriftResult(
                status="check_failed", drift_seconds=None, threshold_seconds=warn_threshold_seconds,
                detail=f"reference clock took {elapsed:.3f}s, exceeding the {timeout_seconds}s budget",
                checked_at=datetime.now(timezone.utc).isoformat(),
            )
        local_now = datetime.now(timezone.utc)
        if reference_now.tzinfo is None:
            reference_now = reference_now.replace(tzinfo=timezone.utc)
        drift = abs((local_now - reference_now).total_seconds())
    except Exception as exc:  # noqa: BLE001 -- a broken reference clock must never propagate
        return ClockDriftResult(
            status="check_failed", drift_seconds=None, threshold_seconds=warn_threshold_seconds,
            detail=f"reference clock check failed: {type(exc).__name__}: {exc}",
            checked_at=datetime.now(timezone.utc).isoformat(),
        )

    status = "drift_detected" if drift > warn_threshold_seconds else "in_sync"
    return ClockDriftResult(
        status=status, drift_seconds=drift, threshold_seconds=warn_threshold_seconds,
        detail=f"local clock differs from reference by {drift:.3f}s (threshold {warn_threshold_seconds}s)",
        checked_at=datetime.now(timezone.utc).isoformat(),
    )


def configured_warn_threshold_seconds() -> float:
    """Reads TIME_SYNC_WARN_THRESHOLD_SECONDS if set, else the default --
    never raises on a malformed value (falls back to the default)."""
    raw = os.environ.get("TIME_SYNC_WARN_THRESHOLD_SECONDS", "").strip()
    if not raw:
        return DEFAULT_WARN_THRESHOLD_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_WARN_THRESHOLD_SECONDS
