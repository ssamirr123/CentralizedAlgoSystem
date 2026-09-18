"""
Phase 15D.8 -- production-safety startup guard.

Closes the gap Phase 15D-AUDIT/15D.7 left open: `KILL_SWITCH_PERSISTENCE_PATH`
and `AUDIT_DB_PATH` (trading/api/execution_state.py) are opt-in env vars.
A production deployment that simply forgets to set either one silently
runs with an IN-MEMORY-ONLY kill switch (resets to DISENGAGED on every
restart -- the dangerous direction) and/or a lost audit trail on
restart. This module makes that failure mode loud instead of silent: in
a production-like environment, `check_production_safety_paths()` raises
`ProductionSafetyError` at startup, before the app ever serves traffic,
rather than starting degraded.

Phase 15D.9 fix: this guard originally had its OWN copy of "what is the
current environment" (ENVIRONMENT, then ENV, default "development"),
duplicating trading/common/deployment_info.py's identical precedence.
That duplication is exactly how this guard ended up silently inert
against the real deployment: the actual `docker-compose.prod.yml` /
`docker-compose.yml` set `APP_ENV`, never `ENVIRONMENT`/`ENV`, so
`is_production_environment()` always saw "development" in production
and this guard never actually ran its check. The fix is not "add a
third variable here too" -- it is to stop duplicating the resolution
logic at all: `is_production_environment()` now calls
`trading.common.deployment_info.resolve_environment()`, the ONE place
this project decides what "environment" means, which now checks
APP_ENV first (see that module for why). There is exactly one
environment-resolution function in this codebase after this fix, not two.

Every dev/test/CI run (which never sets APP_ENV/ENVIRONMENT/ENV to a
production-like value) sees ZERO behavior change -- this guard is a
no-op unless the environment says otherwise. It never touches a broker,
never places an order, never grants a live authorization; it only
inspects environment variables and the filesystem.
"""
from __future__ import annotations

import os
from pathlib import Path

from trading.common.deployment_info import resolve_environment

_PRODUCTION_ENVIRONMENT_VALUES = frozenset({"production", "prod"})

# The two env vars trading/api/execution_state.py already reads to opt
# safety-critical persistence in -- this module does not introduce a
# third, competing configuration surface.
KILL_SWITCH_PATH_ENV_VAR = "KILL_SWITCH_PERSISTENCE_PATH"
AUDIT_DB_PATH_ENV_VAR = "AUDIT_DB_PATH"


class ProductionSafetyError(RuntimeError):
    """Raised at startup when a production-like environment is missing a
    required safety-persistence path. Deliberately never caught or
    suppressed within this module -- the caller (trading/api/app.py's
    lifespan()) is expected to let this propagate and fail process
    startup loudly, rather than start degraded."""


def is_production_environment(environment: str | None = None) -> bool:
    env = (environment if environment is not None else resolve_environment()).strip().lower()
    return env in _PRODUCTION_ENVIRONMENT_VALUES


def _path_is_usable(raw_path: str) -> tuple[bool, str]:
    """Return (ok, reason). A usable path: non-empty, and its parent
    directory exists and is writable -- the file itself need not exist
    yet (every store here creates its own file on first write)."""
    if not raw_path or not raw_path.strip():
        return False, "not set"
    path = Path(raw_path).expanduser()
    parent = path.parent if str(path.parent) else Path(".")
    if not parent.exists():
        return False, f"parent directory does not exist: {parent}"
    if not os.access(parent, os.W_OK):
        return False, f"parent directory is not writable: {parent}"
    return True, "ok"


def check_production_safety_paths(*, environment: str | None = None) -> None:
    """The one function trading/api/app.py's lifespan() calls at startup,
    as early as possible (before init_db(), before the watcher/scheduler
    start). No-op outside a production-like environment. Raises
    ProductionSafetyError -- never returns a boolean a caller could
    silently ignore -- when running in production and either required
    path is missing or unusable."""
    if not is_production_environment(environment):
        return

    problems: list[str] = []
    for env_var in (KILL_SWITCH_PATH_ENV_VAR, AUDIT_DB_PATH_ENV_VAR):
        ok, reason = _path_is_usable(os.environ.get(env_var, ""))
        if not ok:
            problems.append(f"{env_var}: {reason}")

    if problems:
        raise ProductionSafetyError(
            "Refusing to start in a production environment with unsafe persistence configuration: "
            + "; ".join(problems)
            + f". Both {KILL_SWITCH_PATH_ENV_VAR} and {AUDIT_DB_PATH_ENV_VAR} must point to a writable "
              "location in production, or the kill switch resets to DISENGAGED and the audit trail is "
              "lost on every restart."
        )
