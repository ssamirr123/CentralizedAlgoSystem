"""
Phase 15D-DR (Area A): deployment/version identity, computed once per
process and exposed to the startup log and the health/readiness endpoints.

CRITICAL: nothing in this module has any bearing on trading authorization.
A successful, healthy deployment must never be read as `LIVE_AUTHORIZED` --
see trading/api/health.py's `/api/ready` endpoint, which reports broker
connectivity and trading authorization as explicitly SEPARATE fields from
plain application health, specifically so the two are never conflated.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def resolve_environment() -> str:
    """Phase 15D.9: the ONE place this project decides what "environment"
    means -- consulted by `get_deployment_info()` below AND by
    `trading.common.production_guard.check_production_safety_paths()`
    (previously each had its own, independently-drifted copy of this
    same precedence, which is exactly how Phase 15D.8's production guard
    ended up checking a variable name -- ENVIRONMENT/ENV -- that the real
    `docker-compose.prod.yml` never actually sets).

    Precedence: `APP_ENV` first -- this is the variable the real
    deployment files (`docker-compose.yml`, `docker-compose.prod.yml`)
    and `trading/core/config.py::Settings.app_env` (already consulted by
    `trading/api/deps.py` for its own "is this development" fail-closed
    check) actually set -- then `ENVIRONMENT`, then `ENV` (kept for
    backward compatibility with anything already relying on this
    module's older, APP_ENV-blind precedent). Defaults to
    `"development"`, matching every existing caller's prior behavior
    when none of these three are set."""
    for name in ("APP_ENV", "ENVIRONMENT", "ENV"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return "development"


@dataclass(frozen=True)
class DeploymentInfo:
    app_version: str
    git_sha: str
    deployment_id: str
    environment: str
    startup_timestamp: str


def _read_version_file() -> str:
    version_file = _REPO_ROOT / "VERSION"
    if version_file.is_file():
        return version_file.read_text(encoding="utf-8").strip()
    return "0.0.0-dev"


def _resolve_git_sha() -> str:
    """Best-effort: prefer a real `git rev-parse`, since it reflects the
    ACTUAL checked-out commit even if a deployment forgot to stamp a
    version file. Falls back to the GIT_SHA env var (set by many CI/CD
    pipelines that ship a repo without a .git directory), then "unknown"
    -- never raises, since a missing SHA must never block startup."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=3, check=True,
        )
        sha = result.stdout.strip()
        if sha:
            return sha
    except Exception:  # noqa: BLE001 -- git absent/not-a-repo/timeout all fail soft here
        pass
    return os.environ.get("GIT_SHA", "unknown").strip() or "unknown"


def _resolve_deployment_id() -> str:
    """No orchestrator-specific assumption -- reads whichever of these
    common env vars is set (Kubernetes, ECS, generic CI, ...), else falls
    back to a per-process placeholder that is at least stable for the life
    of this process (never blocks startup, never invents a false identity)."""
    for name in ("DEPLOYMENT_ID", "K8S_POD_NAME", "ECS_TASK_ID", "HOSTNAME"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return f"local-{os.getpid()}"


@lru_cache(maxsize=1)
def get_deployment_info() -> DeploymentInfo:
    """Computed once per process (the git SHA/version/deployment ID cannot
    change during a process's lifetime) -- cached so repeated calls (e.g.
    every health-check request) never re-shell out to git."""
    return DeploymentInfo(
        app_version=_read_version_file(),
        git_sha=_resolve_git_sha(),
        deployment_id=_resolve_deployment_id(),
        environment=resolve_environment(),
        startup_timestamp=datetime.now(timezone.utc).isoformat(),
    )


def log_startup_banner(logger) -> None:  # noqa: ANN001 -- accepts any stdlib-shaped Logger
    """The one required startup log line (Area A.4): version, Git SHA,
    deployment ID, environment, and timestamp -- and nothing about trading
    authorization, which this module has no opinion on."""
    info = get_deployment_info()
    logger.info(
        "startup: app_version=%s git_sha=%s deployment_id=%s environment=%s timestamp=%s "
        "(this log line does NOT authorize trading)",
        info.app_version, info.git_sha, info.deployment_id, info.environment, info.startup_timestamp,
    )
