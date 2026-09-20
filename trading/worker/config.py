"""Phase 16.12 -- worker process configuration, entirely from environment
variables (Section 11). No broker trading credential belongs here."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class WorkerConfig:
    tcc_url: str
    worker_id: str
    worker_name: str
    auth_secret: str
    strategy_id: str
    heartbeat_interval_seconds: float
    app_version: str
    git_sha: str
    host_identity: str
    request_timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        return cls(
            tcc_url=_env("TCC_URL", "http://127.0.0.1:8000").rstrip("/"),
            worker_id=_env("WORKER_ID"),
            worker_name=_env("WORKER_NAME", _env("WORKER_ID")),
            auth_secret=_env("WORKER_AUTH_SECRET"),
            strategy_id=_env("STRATEGY_ID"),
            heartbeat_interval_seconds=float(_env("HEARTBEAT_INTERVAL_SECONDS", "10")),
            app_version=_env("APP_VERSION", "0.0.0-dev"),
            git_sha=_env("GIT_SHA", "unknown"),
            host_identity=_env("HOST_IDENTITY", _env("HOSTNAME", "")),
        )
