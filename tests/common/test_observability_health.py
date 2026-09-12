"""Phase 14.6 Blocker C: trading/common/observability.py's
ObservabilityHealth -- proves an observability-layer failure never
propagates, is recorded, and is surfaced separately from any order
result. See tests/common/test_execute_observability_isolation.py for the
end-to-end proof inside StrategyExecutionEngine.execute()."""
from __future__ import annotations

from trading.common.observability import ObservabilityFailure, ObservabilityHealth


def test_healthy_by_default():
    health = ObservabilityHealth()
    assert health.healthy is True
    assert health.failures() == []


def test_safe_observe_runs_a_successful_callable_normally():
    calls = []
    health = ObservabilityHealth()
    health.safe_observe("metrics", "record_order_intent", lambda: calls.append(1))
    assert calls == [1]
    assert health.healthy is True


def test_safe_observe_swallows_an_exception_and_records_it():
    health = ObservabilityHealth()

    def _boom():
        raise RuntimeError("audit trail is on fire")

    health.safe_observe("audit_trail", "append", _boom)  # must not raise

    assert health.healthy is False
    failures = health.failures()
    assert len(failures) == 1
    assert isinstance(failures[0], ObservabilityFailure)
    assert failures[0].component == "audit_trail"
    assert failures[0].operation == "append"
    assert "audit trail is on fire" in failures[0].error


def test_multiple_failures_are_all_recorded():
    health = ObservabilityHealth()
    health.safe_observe("metrics", "increment", lambda: (_ for _ in ()).throw(ValueError("m")))
    health.safe_observe("alerts", "raise", lambda: (_ for _ in ()).throw(TypeError("a")))
    assert health.healthy is False
    assert len(health.failures()) == 2
    assert {f.component for f in health.failures()} == {"metrics", "alerts"}


def test_failures_are_bounded():
    health = ObservabilityHealth(max_failures=3)
    for i in range(10):
        health.safe_observe("metrics", "op", lambda i=i: (_ for _ in ()).throw(RuntimeError(str(i))))
    assert len(health.failures()) == 3


def test_record_directly_without_going_through_safe_observe():
    health = ObservabilityHealth()
    health.record("alerts", "kill_switch", RuntimeError("boom"))
    assert health.healthy is False
    assert health.failures()[0].component == "alerts"
