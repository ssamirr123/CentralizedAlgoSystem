"""
Phase 16.9: minimal worker identity model for the distributed strategy
worker foundation. A "worker" is a process (in this phase, always local/
in-process -- no EC2 instance is created, no network transport exists
yet) that evaluates one or more assigned strategies and submits
OrderIntents to the central WorkerCoordinator. A worker never executes an
order itself -- see trading/common/worker_coordinator.py's own docstring
for the full boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

__all__ = ["WorkerStatus", "WorkerInfo"]


class WorkerStatus(str, Enum):
    REGISTERED = "REGISTERED"
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkerInfo:
    worker_id: str
    name: str
    status: WorkerStatus = WorkerStatus.REGISTERED
    assigned_strategy_ids: tuple[str, ...] = ()
    last_heartbeat_at: str = ""
    started_at: str = field(default_factory=_now)
    version: str = ""
    git_sha: str = ""
    host_identity: str = ""
    # Phase 16.9 Section 13 (duplicate worker protection): a fresh value
    # minted on every successful register_worker() call. A heartbeat or
    # submission carrying a stale/mismatched session_id is rejected --
    # this is how a second accidental process claiming the same
    # worker_id is detected, without any real network/consensus
    # infrastructure.
    session_id: str = ""
