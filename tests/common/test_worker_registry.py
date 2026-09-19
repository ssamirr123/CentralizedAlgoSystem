"""Phase 16.9: trading/common/worker_registry.py -- the central,
authoritative record of worker identity, online/offline state, and
strategy ownership. Never grants trading authority by itself."""
from __future__ import annotations

import datetime as dt

import pytest

from trading.common.worker_identity import WorkerStatus
from trading.common.worker_registry import (
    DuplicateWorkerSessionError,
    StrategyAlreadyOwnedError,
    UnknownWorkerError,
    WorkerRegistry,
)


class _FakeClock:
    def __init__(self, start: dt.datetime):
        self._now = start

    def __call__(self) -> dt.datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += dt.timedelta(seconds=seconds)


def _registry(timeout=30.0):
    clock = _FakeClock(dt.datetime(2026, 9, 19, 9, 0, 0, tzinfo=dt.timezone.utc))
    return WorkerRegistry(heartbeat_timeout_seconds=timeout, clock=clock), clock


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def test_register_worker_creates_an_online_worker_with_a_session_id():
    registry, clock = _registry()
    info = registry.register_worker(worker_id="w1", name="Worker One", version="1.0", git_sha="abc123")
    assert info.status == WorkerStatus.ONLINE
    assert info.session_id != ""
    assert info.assigned_strategy_ids == ()


def test_get_unknown_worker_raises():
    registry, clock = _registry()
    with pytest.raises(UnknownWorkerError):
        registry.get_worker("nope")


# --------------------------------------------------------------------------- #
# Duplicate worker protection (Section 13)
# --------------------------------------------------------------------------- #
def test_registering_the_same_worker_id_twice_while_online_is_refused():
    registry, clock = _registry()
    registry.register_worker(worker_id="w1", name="Worker One")
    with pytest.raises(DuplicateWorkerSessionError):
        registry.register_worker(worker_id="w1", name="Worker One (impostor)")


def test_reregistering_after_going_offline_is_allowed_with_a_fresh_session():
    registry, clock = _registry()
    first = registry.register_worker(worker_id="w1", name="Worker One")
    registry.mark_offline("w1")
    second = registry.register_worker(worker_id="w1", name="Worker One")
    assert second.session_id != first.session_id
    assert second.status == WorkerStatus.ONLINE


def test_heartbeat_with_mismatched_session_id_is_rejected():
    registry, clock = _registry()
    registry.register_worker(worker_id="w1", name="Worker One")
    with pytest.raises(DuplicateWorkerSessionError):
        registry.record_heartbeat("w1", session_id="not-the-real-session")


# --------------------------------------------------------------------------- #
# Heartbeat / staleness
# --------------------------------------------------------------------------- #
def test_heartbeat_keeps_worker_online():
    registry, clock = _registry(timeout=30.0)
    registry.register_worker(worker_id="w1", name="Worker One")
    clock.advance(20)
    registry.record_heartbeat("w1")
    clock.advance(20)
    assert registry.get_worker("w1").status == WorkerStatus.ONLINE  # 20s since last heartbeat, under 30s timeout


def test_stale_heartbeat_marks_worker_offline():
    registry, clock = _registry(timeout=30.0)
    registry.register_worker(worker_id="w1", name="Worker One")
    clock.advance(31)
    assert registry.get_worker("w1").status == WorkerStatus.OFFLINE


def test_list_workers_recomputes_staleness_for_all():
    registry, clock = _registry(timeout=30.0)
    registry.register_worker(worker_id="w1", name="A")
    registry.register_worker(worker_id="w2", name="B")
    clock.advance(31)
    statuses = {w.worker_id: w.status for w in registry.list_workers()}
    assert statuses == {"w1": WorkerStatus.OFFLINE, "w2": WorkerStatus.OFFLINE}


def test_manually_marked_offline_worker_stays_offline():
    registry, clock = _registry()
    registry.register_worker(worker_id="w1", name="Worker One")
    registry.mark_offline("w1")
    assert registry.get_worker("w1").status == WorkerStatus.OFFLINE


# --------------------------------------------------------------------------- #
# Strategy ownership -- fail closed on duplicate active ownership (Section 4)
# --------------------------------------------------------------------------- #
def test_assign_strategy_to_a_worker():
    registry, clock = _registry()
    registry.register_worker(worker_id="w1", name="Worker One")
    registry.assign_strategy("StrategyA", "w1")
    assert registry.get_strategy_owner("StrategyA") == "w1"
    assert "StrategyA" in registry.get_worker("w1").assigned_strategy_ids


def test_assign_strategy_to_unknown_worker_raises():
    registry, clock = _registry()
    with pytest.raises(UnknownWorkerError):
        registry.assign_strategy("StrategyA", "nope")


def test_cannot_assign_a_strategy_already_actively_owned_by_another_online_worker():
    registry, clock = _registry()
    registry.register_worker(worker_id="w1", name="A")
    registry.register_worker(worker_id="w2", name="B")
    registry.assign_strategy("StrategyA", "w1")
    with pytest.raises(StrategyAlreadyOwnedError):
        registry.assign_strategy("StrategyA", "w2")


def test_can_reassign_a_strategy_once_the_previous_owner_is_offline():
    registry, clock = _registry()
    registry.register_worker(worker_id="w1", name="A")
    registry.register_worker(worker_id="w2", name="B")
    registry.assign_strategy("StrategyA", "w1")
    registry.mark_offline("w1")
    registry.assign_strategy("StrategyA", "w2")  # must not raise
    assert registry.get_strategy_owner("StrategyA") == "w2"


def test_unassign_strategy_clears_ownership():
    registry, clock = _registry()
    registry.register_worker(worker_id="w1", name="A")
    registry.assign_strategy("StrategyA", "w1")
    registry.unassign_strategy("StrategyA")
    assert registry.get_strategy_owner("StrategyA") is None
    assert "StrategyA" not in registry.get_worker("w1").assigned_strategy_ids


# --------------------------------------------------------------------------- #
# Restart safety (Section 11) -- registry-level: restart never implies
# strategy ownership is silently revoked or reassigned.
# --------------------------------------------------------------------------- #
def test_worker_restart_preserves_its_prior_strategy_assignments():
    registry, clock = _registry()
    registry.register_worker(worker_id="w1", name="A")
    registry.assign_strategy("StrategyA", "w1")
    registry.mark_offline("w1")  # simulate a crash/reboot
    restarted = registry.register_worker(worker_id="w1", name="A")
    assert "StrategyA" in restarted.assigned_strategy_ids
    assert registry.get_strategy_owner("StrategyA") == "w1"
