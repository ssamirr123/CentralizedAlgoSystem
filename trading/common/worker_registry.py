"""
WorkerRegistry -- Phase 16.9: the central, authoritative record of which
worker processes exist, their online/offline state, and which strategies
each currently owns.

Never grants trading authority by itself. Registering a worker or
recording its heartbeat is purely operational bookkeeping -- see
trading/common/worker_coordinator.py for the actual OrderIntent
validation chain, which independently re-checks worker/ownership state
from this registry rather than assuming a prior ONLINE status is still
current or was ever sufficient for anything beyond routing.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Iterable

from trading.common.worker_identity import WorkerInfo, WorkerStatus

__all__ = [
    "DEFAULT_HEARTBEAT_TIMEOUT_SECONDS", "UnknownWorkerError", "DuplicateWorkerSessionError",
    "StrategyAlreadyOwnedError", "WorkerRegistry",
]

DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 30.0


class UnknownWorkerError(KeyError):
    """Raised for any worker_id this registry has never seen."""


class DuplicateWorkerSessionError(RuntimeError):
    """Raised when a worker_id registers while a DIFFERENT session for
    that same worker_id is already ONLINE (Section 13's duplicate-worker
    protection), or when a heartbeat/submission carries a session_id that
    does not match the worker's current active session. Fail closed: the
    new/stale caller is refused; the existing session is left alone."""


class StrategyAlreadyOwnedError(RuntimeError):
    """Raised when assign_strategy() targets a strategy already actively
    owned by a DIFFERENT ONLINE worker (Section 4's "fail closed on
    duplicate active ownership")."""


class WorkerRegistry:
    def __init__(
        self, *, heartbeat_timeout_seconds: float = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._workers: dict[str, WorkerInfo] = {}
        self._strategy_owner: dict[str, str] = {}
        self._heartbeat_timeout = heartbeat_timeout_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def register_worker(
        self, *, worker_id: str, name: str, version: str = "", git_sha: str = "", host_identity: str = "",
    ) -> WorkerInfo:
        with self._lock:
            existing = self._workers.get(worker_id)
            if existing is not None:
                self._recompute_status_locked(worker_id)
                existing = self._workers[worker_id]
                if existing.status == WorkerStatus.ONLINE:
                    raise DuplicateWorkerSessionError(
                        f"worker_id {worker_id!r} is already ONLINE (session {existing.session_id!r}); "
                        "refusing a second concurrent registration"
                    )
            info = WorkerInfo(
                worker_id=worker_id, name=name, status=WorkerStatus.ONLINE, version=version,
                git_sha=git_sha, host_identity=host_identity, session_id=str(uuid.uuid4()),
                last_heartbeat_at=self._clock().isoformat(),
                assigned_strategy_ids=existing.assigned_strategy_ids if existing else (),
            )
            self._workers[worker_id] = info
            return info

    def get_worker(self, worker_id: str) -> WorkerInfo:
        with self._lock:
            self._recompute_status_locked(worker_id)
            try:
                return self._workers[worker_id]
            except KeyError:
                raise UnknownWorkerError(f"No such worker: {worker_id!r}") from None

    def list_workers(self) -> list[WorkerInfo]:
        with self._lock:
            for wid in list(self._workers):
                self._recompute_status_locked(wid)
            return list(self._workers.values())

    def record_heartbeat(
        self, worker_id: str, *, session_id: str | None = None, strategy_ids: Iterable[str] | None = None,
        runtime_state: str | None = None,
    ) -> WorkerInfo:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise UnknownWorkerError(f"No such worker: {worker_id!r}")
            if session_id is not None and session_id != worker.session_id:
                raise DuplicateWorkerSessionError(
                    f"heartbeat session_id {session_id!r} does not match the current active session "
                    f"{worker.session_id!r} for worker {worker_id!r} -- stale or duplicate process"
                )
            worker.last_heartbeat_at = self._clock().isoformat()
            if worker.status not in (WorkerStatus.STOPPED,):
                worker.status = WorkerStatus.DEGRADED if runtime_state == "DEGRADED" else WorkerStatus.ONLINE
            if strategy_ids is not None:
                worker.assigned_strategy_ids = tuple(strategy_ids)
            return worker

    def mark_offline(self, worker_id: str) -> WorkerInfo:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise UnknownWorkerError(f"No such worker: {worker_id!r}")
            worker.status = WorkerStatus.OFFLINE
            return worker

    def mark_stopped(self, worker_id: str) -> WorkerInfo:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise UnknownWorkerError(f"No such worker: {worker_id!r}")
            worker.status = WorkerStatus.STOPPED
            return worker

    def assign_strategy(self, strategy_id: str, worker_id: str) -> None:
        with self._lock:
            if worker_id not in self._workers:
                raise UnknownWorkerError(f"No such worker: {worker_id!r}")
            current_owner = self._strategy_owner.get(strategy_id)
            if current_owner is not None and current_owner != worker_id:
                owner_info = self._workers.get(current_owner)
                if owner_info is not None and owner_info.status == WorkerStatus.ONLINE:
                    raise StrategyAlreadyOwnedError(
                        f"strategy {strategy_id!r} is already actively owned by worker {current_owner!r}"
                    )
            self._strategy_owner[strategy_id] = worker_id
            worker = self._workers[worker_id]
            if strategy_id not in worker.assigned_strategy_ids:
                worker.assigned_strategy_ids = worker.assigned_strategy_ids + (strategy_id,)

    def unassign_strategy(self, strategy_id: str) -> None:
        with self._lock:
            owner = self._strategy_owner.pop(strategy_id, None)
            if owner is not None and owner in self._workers:
                worker = self._workers[owner]
                worker.assigned_strategy_ids = tuple(s for s in worker.assigned_strategy_ids if s != strategy_id)

    def get_strategy_owner(self, strategy_id: str) -> str | None:
        with self._lock:
            return self._strategy_owner.get(strategy_id)

    def _recompute_status_locked(self, worker_id: str) -> None:
        worker = self._workers.get(worker_id)
        if worker is None or worker.status in (WorkerStatus.OFFLINE, WorkerStatus.STOPPED):
            return
        if not worker.last_heartbeat_at:
            return
        last = datetime.fromisoformat(worker.last_heartbeat_at)
        age = (self._clock() - last).total_seconds()
        if age > self._heartbeat_timeout:
            worker.status = WorkerStatus.OFFLINE
