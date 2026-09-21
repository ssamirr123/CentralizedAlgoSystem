"""
Worker protocol -- Phase 16.9: a transport-independent, serializable set
of logical messages exchanged between a strategy worker and the central
WorkerCoordinator (trading/common/worker_coordinator.py).

This phase deliberately does NOT introduce Kafka, RabbitMQ, Redis, or
Celery -- nothing already in this repository requires one, and the local
simulation (see docs/phase-16-9-distributed-strategy-worker-foundation-report.md
Section 15) exercises this exact protocol via direct in-process Python
calls, which the same plain dataclasses below would carry unchanged over
any future real transport (HTTP, a queue, anything) without themselves
needing to change.

None of these messages may ever carry a broker credential, an API key or
secret, an access/session/refresh token, or a LiveAuthorization token --
there is no field for any of them anywhere below, enforced structurally
by tests/common/test_worker_protocol.py, which scans every field name.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from trading.common.execution import ExecutionResult
from trading.common.order_intent import OrderIntent

__all__ = [
    "WorkerRegistration", "WorkerHeartbeat", "StrategyStartCommand", "StrategyStopCommand",
    "StrategyEvaluateCommand", "StrategyRuntimeUpdate", "OrderIntentSubmission", "OrderIntentResult",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class WorkerRegistration:
    worker_id: str
    name: str
    version: str = ""
    git_sha: str = ""
    host_identity: str = ""


@dataclass(frozen=True)
class WorkerHeartbeat:
    worker_id: str
    session_id: str
    strategy_ids: tuple[str, ...]
    runtime_state: str
    timestamp: str = field(default_factory=_now)


@dataclass(frozen=True)
class StrategyStartCommand:
    strategy_id: str
    worker_id: str
    requested_by: str = ""


@dataclass(frozen=True)
class StrategyStopCommand:
    strategy_id: str
    worker_id: str
    requested_by: str = ""


@dataclass(frozen=True)
class StrategyEvaluateCommand:
    strategy_id: str
    worker_id: str


@dataclass(frozen=True)
class StrategyRuntimeUpdate:
    strategy_id: str
    worker_id: str
    runtime_state: str
    market_data_status: str = ""
    last_error: str = ""
    timestamp: str = field(default_factory=_now)


@dataclass(frozen=True)
class OrderIntentSubmission:
    """What a worker sends the central TCC -- carries the existing,
    unmodified broker-independent OrderIntent plus enough metadata
    (Section 12: worker_id, evaluation_id, generated_at, submission_id)
    for WorkerCoordinator to detect a stale or replayed submission
    before ever consulting the central safety gates."""

    worker_id: str
    session_id: str
    strategy_id: str
    evaluation_id: str
    intent: OrderIntent
    generated_at: str = field(default_factory=_now)
    submission_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class OrderIntentResult:
    submission_id: str
    accepted: bool
    execution_result: ExecutionResult | None
    reason: str = ""
