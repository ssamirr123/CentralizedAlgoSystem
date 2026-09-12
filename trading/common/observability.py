"""
Phase 13 -- centralized observability for the broker-agnostic execution
framework: metrics, structured logging, and a tamper-evident audit trail
that makes every order's full lifecycle traceable by correlation_id:

    Strategy -> OrderIntent -> Risk Decision -> Execution -> Broker Order
        -> Fill -> Position -> P&L

Nothing in this module calls a broker, places an order, or has any
bearing on whether a real order is placed -- it only records what
happened elsewhere. Every collaborator (StrategyExecutionEngine,
BaseStrategy, BrokerManager) that can emit observability events takes
these as OPTIONAL constructor kwargs defaulting to None, so every
existing caller (the dozens of Phase 1-12 tests that construct these
classes without knowing this module exists) keeps working identically --
Phase 13 is purely additive instrumentation, never a required dependency.

No process-wide global instance is created here (mirroring
BrokerManager()/RiskManager()/StrategyRegistry()'s own convention of "the
caller constructs and owns the instance") -- a hidden global would
reintroduce the exact cross-test state-leakage hazard Phase 11's
execution_state.py was designed around avoiding.

--------------------------------------------------------------------------
MetricsRegistry
--------------------------------------------------------------------------
A thread-safe counters/gauges/latency-samples store, plus one named
helper method per metric this phase asks for (record_order_intent(),
record_simulated_fill(), etc.) so call sites never have to remember a raw
metric-name string.

--------------------------------------------------------------------------
AuditTrail -- "an audit trail that cannot be silently modified"
--------------------------------------------------------------------------
An append-only, hash-chained event log: every record's hash commits to
its own content AND the previous record's hash, the same construction
git commits and blockchains use. verify() recomputes every hash from
scratch and detects ANY modification, reordering, or deletion of a past
record.

Honest framing: this is an in-process Python object. It cannot physically
prevent a process with direct memory access from mutating the underlying
list -- no purely in-memory, single-process structure can make that
impossible. What the hash chain provides is TAMPER EVIDENCE: any such
mutation is immediately, deterministically detectable via verify(),
which is the realistic and standard meaning of "cannot be silently
modified" for an application-level audit trail (the same guarantee a git
history or a blockchain provides -- rewriting history is detectable, not
physically prevented). A future phase adding durable storage (e.g.
write-once object storage, or a database with a write-only role and a
separate verifier) would extend this same hash-chain construction to
also resist a compromised process, not just an honest one.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

_log = logging.getLogger("trading.observability")

# --------------------------------------------------------------------------
# Event / alert vocabulary -- centralized string constants, same
# "not an enum, routes pass literals, centralised here for grep-ability"
# convention trading/api/security/audit.py already established.
# --------------------------------------------------------------------------
EVENT_ORDER_INTENT_CREATED = "ORDER_INTENT_CREATED"
EVENT_RISK_DECISION = "RISK_DECISION"
EVENT_LIVE_CANARY_AUTHORIZATION = "LIVE_CANARY_AUTHORIZATION"
EVENT_EXECUTION_RESULT = "EXECUTION_RESULT"
EVENT_BROKER_ORDER_PLACED = "BROKER_ORDER_PLACED"
EVENT_FILL = "FILL"
EVENT_POSITION_UPDATE = "POSITION_UPDATE"
EVENT_PNL_UPDATE = "PNL_UPDATE"
EVENT_STRATEGY_STARTED = "STRATEGY_STARTED"
EVENT_STRATEGY_STOPPED = "STRATEGY_STOPPED"
EVENT_BROKER_CONNECTED = "BROKER_CONNECTED"
EVENT_BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
EVENT_ERROR = "ERROR"

# The full, in-order lifecycle this phase's brief names -- exposed so a
# caller (or a test) can assert a trace visits every stage it reached in
# this exact order, without hand-copying the list everywhere.
ORDER_LIFECYCLE_STAGES = (
    EVENT_ORDER_INTENT_CREATED,
    EVENT_RISK_DECISION,
    EVENT_EXECUTION_RESULT,
    EVENT_BROKER_ORDER_PLACED,
    EVENT_FILL,
    EVENT_POSITION_UPDATE,
    EVENT_PNL_UPDATE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MetricsSnapshot:
    """Point-in-time, immutable copy returned by MetricsRegistry.snapshot()
    -- safe to hand to an API response without holding the registry's lock."""

    counters: dict[str, int]
    gauges: dict[str, float]
    statuses: dict[str, str]
    latencies: dict[str, list[float]]


class MetricsRegistry:
    """Thread-safe counters/gauges/status-strings/latency-samples, plus one
    named helper per metric this phase's brief asks for. Every counter and
    gauge is recorded both per-entity (e.g. "order_intents:StrategyA") and
    in a "_all" aggregate, so a caller can read either the per-strategy
    breakdown or the fleet-wide total without re-deriving it."""

    def __init__(self, max_latency_samples: int = 500) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = {}
        self._statuses: dict[str, str] = {}
        self._latencies: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=max_latency_samples))

    # -- primitives ---------------------------------------------------------- #
    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def set_status(self, name: str, value: str) -> None:
        with self._lock:
            self._statuses[name] = value

    def observe_latency(self, name: str, seconds: float) -> None:
        with self._lock:
            self._latencies[name].append(seconds)

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            return MetricsSnapshot(
                counters=dict(self._counters),
                gauges=dict(self._gauges),
                statuses=dict(self._statuses),
                latencies={k: list(v) for k, v in self._latencies.items()},
            )

    # -- named helpers, one per metric this phase's brief lists -------------- #
    def record_strategy_heartbeat(self, strategy_id: str) -> None:
        self.set_gauge(f"strategy_heartbeat:{strategy_id}", datetime.now(timezone.utc).timestamp())

    def record_broker_heartbeat(self, broker_id: str) -> None:
        self.set_gauge(f"broker_heartbeat:{broker_id}", datetime.now(timezone.utc).timestamp())

    def record_execution_latency(self, strategy_id: str, seconds: float) -> None:
        self.observe_latency(f"execution_latency:{strategy_id}", seconds)
        self.observe_latency("execution_latency:_all", seconds)

    def record_order_intent(self, strategy_id: str) -> None:
        self.increment(f"order_intents:{strategy_id}")
        self.increment("order_intents:_all")

    def record_order_approved(self, strategy_id: str) -> None:
        self.increment(f"orders_approved:{strategy_id}")
        self.increment("orders_approved:_all")

    def record_order_rejected(self, strategy_id: str, reason: str = "") -> None:  # noqa: ARG002 - kept for call-site clarity
        self.increment(f"orders_rejected:{strategy_id}")
        self.increment("orders_rejected:_all")

    def record_simulated_fill(self, strategy_id: str) -> None:
        self.increment(f"simulated_fills:{strategy_id}")
        self.increment("simulated_fills:_all")

    def record_real_fill(self, strategy_id: str) -> None:
        self.increment(f"real_fills:{strategy_id}")
        self.increment("real_fills:_all")

    def record_error(self, component: str) -> None:
        self.increment(f"errors:{component}")
        self.increment("errors:_all")

    def record_pnl(self, strategy_id: str, value: float) -> None:
        self.set_gauge(f"pnl:{strategy_id}", value)

    def record_exposure(self, strategy_id: str, value: float) -> None:
        self.set_gauge(f"exposure:{strategy_id}", value)

    def record_position(self, strategy_id: str, symbol: str, quantity: int) -> None:
        self.set_gauge(f"position:{strategy_id}:{symbol}", quantity)

    def record_account_status(self, account_id: str, status: str) -> None:
        self.set_status(f"account_status:{account_id}", status)


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------
class TamperDetectedError(RuntimeError):
    """Raised by AuditTrail.verify_or_raise() when the hash chain doesn't
    reconstruct -- a record was modified, reordered, or deleted."""


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    timestamp: str
    event_type: str
    correlation_id: str
    strategy_id: str
    detail: dict[str, Any]
    prev_hash: str
    hash: str


def _record_payload(
    seq: int, timestamp: str, event_type: str, correlation_id: str, strategy_id: str,
    detail: dict[str, Any], prev_hash: str,
) -> str:
    return json.dumps(
        {
            "seq": seq, "timestamp": timestamp, "event_type": event_type,
            "correlation_id": correlation_id, "strategy_id": strategy_id,
            "detail": detail, "prev_hash": prev_hash,
        },
        sort_keys=True, default=str,
    )


class AuditTrail:
    """Append-only, hash-chained event log. See the module docstring's
    "AuditTrail" section for what "cannot be silently modified" means
    here precisely."""

    GENESIS_HASH = "0" * 64

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[AuditRecord] = []

    def append(self, event_type: str, *, correlation_id: str = "", strategy_id: str = "", **detail: Any) -> AuditRecord:
        with self._lock:
            seq = len(self._records)
            prev_hash = self._records[-1].hash if self._records else self.GENESIS_HASH
            timestamp = _now()
            payload = _record_payload(seq, timestamp, event_type, correlation_id, strategy_id, detail, prev_hash)
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            record = AuditRecord(seq, timestamp, event_type, correlation_id, strategy_id, dict(detail), prev_hash, digest)
            self._records.append(record)
        _log.info(
            "[LIFECYCLE] %s",
            {"seq": record.seq, "event_type": event_type, "correlation_id": correlation_id, "strategy_id": strategy_id, **detail},
        )
        return record

    def records(self) -> list[AuditRecord]:
        with self._lock:
            return list(self._records)

    def trace(self, correlation_id: str) -> list[AuditRecord]:
        """Every record sharing one correlation_id, in append order -- the
        full Strategy -> OrderIntent -> Risk Decision -> Execution ->
        Broker Order -> Fill -> Position -> P&L trace for one order."""
        return [r for r in self.records() if r.correlation_id == correlation_id]

    def verify(self) -> bool:
        """Recompute every hash from scratch. False if any record's stored
        hash/prev_hash doesn't match what its own content should produce --
        i.e. something was modified, reordered, or deleted after the fact."""
        prev_hash = self.GENESIS_HASH
        for r in self.records():
            expected_payload = _record_payload(r.seq, r.timestamp, r.event_type, r.correlation_id, r.strategy_id, r.detail, prev_hash)
            expected_hash = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()
            if r.prev_hash != prev_hash or r.hash != expected_hash:
                return False
            prev_hash = r.hash
        return True

    def verify_or_raise(self) -> None:
        if not self.verify():
            raise TamperDetectedError(
                "Audit trail hash chain verification failed -- a record was modified, reordered, or deleted."
            )


# --------------------------------------------------------------------------
# Observability health (Phase 14.6, Blocker C)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ObservabilityFailure:
    """One recorded failure of an observability call (metrics/audit/
    alerts) -- never an ExecutionResult failure. See ObservabilityHealth's
    own docstring for why this distinction exists and matters."""

    component: str  # "metrics" | "audit_trail" | "alerts"
    operation: str
    error: str
    timestamp: str


class ObservabilityHealth:
    """Phase 14.6 Blocker C -- the fix for: a real broker order succeeds,
    then an internal bug in MetricsRegistry/AuditTrail/AlertManager raises,
    and the caller of StrategyExecutionEngine.execute() sees an exception
    instead of the successful ExecutionResult -- risking a retry that
    duplicates a real order.

    ObservabilityHealth.safe_observe() is the ONLY way execute() (and any
    other caller that adopts this pattern) touches metrics/audit/alerts:
    every such call is wrapped so it can NEVER propagate an exception.
    A failure is not silently discarded, though -- it is:
      1. logged immediately via the standard logger (last-resort, always-
         available channel -- mirrors trading/api/security/audit.py's own
         "auditing must never break the request" discipline, generalized
         here to metrics/alerts too), and
      2. recorded in this object's own bounded history, readable via
         failures()/healthy, so a health-check endpoint or monitoring
         script can surface "the observability layer itself is unhealthy"
         as its own, separate signal -- distinct from any single order's
         own success/failure, which this class never touches or reports.

    This class holds no reference to the order/execution path itself; it
    cannot affect whether a broker call happens, and it never raises.
    """

    def __init__(self, max_failures: int = 200) -> None:
        self._lock = threading.Lock()
        self._failures: deque[ObservabilityFailure] = deque(maxlen=max_failures)

    def safe_observe(self, component: str, operation: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 -- observability must never break execution
            self.record(component, operation, exc)

    def record(self, component: str, operation: str, exc: Exception) -> None:
        with self._lock:
            self._failures.append(ObservabilityFailure(component, operation, str(exc), _now()))
        _log.error(
            "[OBSERVABILITY FAILURE] component=%s operation=%s error=%s -- order execution result is UNAFFECTED by this",
            component, operation, exc,
        )

    def failures(self) -> list[ObservabilityFailure]:
        with self._lock:
            return list(self._failures)

    @property
    def healthy(self) -> bool:
        with self._lock:
            return len(self._failures) == 0
