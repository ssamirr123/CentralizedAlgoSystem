"""Phase 15D.8: production deployment & security hardening.

Covers: the production-safety startup guard (kill-switch/audit
persistence enforcement), the /api/ready broker-readiness probe
(bounded, read-only), the clock-drift diagnostic (read-only, opt-in),
file-permission hardening (POSIX 0600) on the safety-critical stores,
and the backup/restore tooling.

No test in this file places a real broker order, connects to a real
broker, or grants a real live authorization.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading.common.audit_store import PersistentAuditTrail
from trading.common.broker import BrokerClient, OrderResult, OrderType, Quote
from trading.common.file_permissions import harden_file_permissions
from trading.common.idempotency_store import SqliteIdempotencyStore
from trading.common.kill_switch import CentralKillSwitch
from trading.common.live_authorization import SqliteLiveAuthorizationStore
from trading.common.production_guard import (
    ProductionSafetyError,
    check_production_safety_paths,
    is_production_environment,
)
from trading.common.reconciliation import SqliteReconciliationStore
from trading.common.time_sync import check_clock_drift, configured_warn_threshold_seconds
from trading.tools.backup_safety_stores import restore_one, snapshot_one, verify_snapshot


class _FakeBroker(BrokerClient):
    is_simulated = True

    def __init__(self, *, connected: bool, raises: bool = False):
        self._connected = connected
        self._raises = raises

    def connect(self) -> None:
        self._connected = True

    def is_connected(self) -> bool:
        if self._raises:
            raise ConnectionError("simulated failure")
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, last_price=1.0, timestamp="2026-01-01T00:00:00+00:00")

    def place_order(self, *a, **k) -> OrderResult:
        raise AssertionError("this fake broker must never be asked to place an order in this test file")

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_positions(self):
        return []


# ======================================================================== #
# Production safety guard
# ======================================================================== #
def test_is_production_environment_recognizes_production_and_prod():
    assert is_production_environment("production")
    assert is_production_environment("PRODUCTION")
    assert is_production_environment("prod")
    assert not is_production_environment("development")
    assert not is_production_environment("docker")
    assert not is_production_environment("staging")
    assert not is_production_environment("")


def test_guard_is_noop_outside_production(tmp_path, monkeypatch):
    monkeypatch.delenv("KILL_SWITCH_PERSISTENCE_PATH", raising=False)
    monkeypatch.delenv("AUDIT_DB_PATH", raising=False)
    check_production_safety_paths(environment="development")  # must not raise
    check_production_safety_paths(environment="docker")  # must not raise
    check_production_safety_paths(environment="")  # must not raise


def test_guard_raises_in_production_with_no_paths_set(monkeypatch):
    monkeypatch.delenv("KILL_SWITCH_PERSISTENCE_PATH", raising=False)
    monkeypatch.delenv("AUDIT_DB_PATH", raising=False)
    with pytest.raises(ProductionSafetyError, match="KILL_SWITCH_PERSISTENCE_PATH"):
        check_production_safety_paths(environment="production")


def test_guard_raises_in_production_with_only_one_path_set(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_PERSISTENCE_PATH", str(tmp_path / "ks.json"))
    monkeypatch.delenv("AUDIT_DB_PATH", raising=False)
    with pytest.raises(ProductionSafetyError, match="AUDIT_DB_PATH"):
        check_production_safety_paths(environment="production")


def test_guard_raises_when_parent_directory_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_PERSISTENCE_PATH", str(tmp_path / "no_such_dir" / "ks.json"))
    monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "audit.db"))
    with pytest.raises(ProductionSafetyError, match="does not exist"):
        check_production_safety_paths(environment="production")


def test_guard_passes_in_production_with_both_paths_set(tmp_path, monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_PERSISTENCE_PATH", str(tmp_path / "ks.json"))
    monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "audit.db"))
    check_production_safety_paths(environment="production")  # must not raise


def test_guard_reads_environ_when_no_environment_kwarg_given(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("KILL_SWITCH_PERSISTENCE_PATH", raising=False)
    monkeypatch.delenv("AUDIT_DB_PATH", raising=False)
    with pytest.raises(ProductionSafetyError):
        check_production_safety_paths()


def test_lifespan_startup_fails_in_production_without_persistence_paths(monkeypatch):
    """End-to-end: the guard is actually wired into trading/api/app.py's
    lifespan(), so a TestClient startup in a 'production' environment
    with no persistence paths configured must fail to start."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("KILL_SWITCH_PERSISTENCE_PATH", raising=False)
    monkeypatch.delenv("AUDIT_DB_PATH", raising=False)
    from fastapi.testclient import TestClient

    from trading.api.app import create_app

    app = create_app()
    with pytest.raises(ProductionSafetyError):
        with TestClient(app):
            pass


def test_lifespan_startup_succeeds_in_production_with_persistence_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("KILL_SWITCH_PERSISTENCE_PATH", str(tmp_path / "ks.json"))
    monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "audit.db"))
    from fastapi.testclient import TestClient

    from trading.api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        r = c.get("/api/health")
        assert r.status_code == 200


# ======================================================================== #
# /api/ready broker-readiness probe
# ======================================================================== #
def test_ready_endpoint_reports_not_configured_by_default(client):
    r = client.get("/api/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["application"] == "ready"
    assert body["broker"] == "not_configured"  # example accounts never attach a broker_client (see execution_state.py)
    assert body["trading_authorized"] is False


def test_ready_endpoint_reports_connected_when_a_broker_is_attached(client, app):
    account = app.state.execution.broker_manager.accounts()[0]
    account.broker_client = _FakeBroker(connected=True)
    r = client.get("/api/ready")
    assert r.json()["broker"] == "connected"


def test_ready_endpoint_reports_not_connected_when_broker_disconnected(client, app):
    account = app.state.execution.broker_manager.accounts()[0]
    account.broker_client = _FakeBroker(connected=False)
    r = client.get("/api/ready")
    assert r.json()["broker"] == "not_connected"


def test_ready_endpoint_fails_safe_when_is_connected_raises(client, app):
    account = app.state.execution.broker_manager.accounts()[0]
    account.broker_client = _FakeBroker(connected=True, raises=True)
    r = client.get("/api/ready")
    assert r.status_code == 200  # never a 500
    assert r.json()["broker"] == "not_connected"


def test_ready_endpoint_never_calls_place_order(client, app):
    account = app.state.execution.broker_manager.accounts()[0]
    broker = _FakeBroker(connected=True)
    account.broker_client = broker
    client.get("/api/ready")  # place_order() would raise AssertionError if called -- see _FakeBroker


def test_broker_readiness_check_is_bounded(client, app):
    class _SlowBroker(_FakeBroker):
        def is_connected(self) -> bool:
            time.sleep(5)  # far longer than _BROKER_READINESS_TIMEOUT_SECONDS
            return True

    account = app.state.execution.broker_manager.accounts()[0]
    account.broker_client = _SlowBroker(connected=True)
    started = time.monotonic()
    r = client.get("/api/ready")
    elapsed = time.monotonic() - started
    assert r.status_code == 200
    assert elapsed < 3.0  # well under the 5s sleep -- the probe did not wait for it
    assert r.json()["broker"] == "not_connected"  # timed out -> fails safe


# ======================================================================== #
# Clock-drift diagnostic
# ======================================================================== #
def test_clock_drift_not_checked_with_no_reference():
    result = check_clock_drift()
    assert result.status == "not_checked"
    assert result.drift_seconds is None


def test_clock_drift_in_sync_with_matching_reference():
    result = check_clock_drift(reference_clock=lambda: datetime.now(timezone.utc))
    assert result.status == "in_sync"
    assert result.drift_seconds is not None
    assert result.drift_seconds < 1.0


def test_clock_drift_detected_with_offset_reference():
    offset_clock = lambda: datetime.now(timezone.utc) - timedelta(seconds=10)  # noqa: E731
    result = check_clock_drift(reference_clock=offset_clock, warn_threshold_seconds=2.0)
    assert result.status == "drift_detected"
    assert result.drift_seconds >= 9.0


def test_clock_drift_check_failed_never_raises():
    def _broken_clock():
        raise RuntimeError("reference unreachable")

    result = check_clock_drift(reference_clock=_broken_clock)
    assert result.status == "check_failed"
    assert result.drift_seconds is None


def test_clock_drift_never_changes_trading_behavior():
    """This is a documentation-as-code assertion: ClockDriftResult has no
    field or method that could gate execution -- it is a pure fact,
    consumed only by a human via /api/health."""
    result = check_clock_drift(reference_clock=lambda: datetime.now(timezone.utc) - timedelta(seconds=100))
    assert result.status == "drift_detected"
    assert not hasattr(result, "engage_kill_switch")
    assert not hasattr(result, "block_trading")


def test_configured_warn_threshold_defaults_and_reads_env(monkeypatch):
    monkeypatch.delenv("TIME_SYNC_WARN_THRESHOLD_SECONDS", raising=False)
    assert configured_warn_threshold_seconds() == 2.0
    monkeypatch.setenv("TIME_SYNC_WARN_THRESHOLD_SECONDS", "5.5")
    assert configured_warn_threshold_seconds() == 5.5
    monkeypatch.setenv("TIME_SYNC_WARN_THRESHOLD_SECONDS", "not-a-number")
    assert configured_warn_threshold_seconds() == 2.0  # falls back, never raises


def test_health_endpoint_exposes_clock_drift_field(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["clock_drift"] == "not_checked"


# ======================================================================== #
# File-permission hardening (POSIX only)
# ======================================================================== #
@pytest.mark.skipif(os.name != "posix", reason="POSIX-only hardening; a documented no-op on Windows")
def test_harden_file_permissions_sets_0600(tmp_path):
    f = tmp_path / "secret.db"
    f.write_text("data")
    assert harden_file_permissions(str(f)) is True
    assert (os.stat(f).st_mode & 0o777) == 0o600


@pytest.mark.skipif(os.name == "posix", reason="Windows-only no-op assertion")
def test_harden_file_permissions_is_noop_on_windows(tmp_path):
    f = tmp_path / "secret.db"
    f.write_text("data")
    assert harden_file_permissions(str(f)) is False


def test_harden_file_permissions_never_raises_for_missing_file(tmp_path):
    assert harden_file_permissions(str(tmp_path / "does_not_exist.db")) is False


def test_harden_file_permissions_never_raises_for_empty_path():
    assert harden_file_permissions("") is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only hardening")
@pytest.mark.parametrize("store_factory", [
    lambda p: SqliteIdempotencyStore(db_path=str(p)),
    lambda p: PersistentAuditTrail(db_path=str(p)),
    lambda p: SqliteReconciliationStore(db_path=str(p)),
    lambda p: SqliteLiveAuthorizationStore(db_path=str(p)),
])
def test_safety_stores_are_created_with_0600_permissions(tmp_path, store_factory):
    db_path = tmp_path / "store.db"
    store_factory(db_path)
    assert (os.stat(db_path).st_mode & 0o777) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only hardening")
def test_kill_switch_persistence_file_has_0600_permissions(tmp_path):
    ks_path = tmp_path / "ks.json"
    ks = CentralKillSwitch(persistence_path=str(ks_path))
    ks.engage(by="test", reason="hardening check")
    assert (os.stat(ks_path).st_mode & 0o777) == 0o600


# ======================================================================== #
# Backup / restore tooling
# ======================================================================== #
def test_snapshot_sqlite_store_round_trips(tmp_path):
    db_path = tmp_path / "idem.db"
    store = SqliteIdempotencyStore(db_path=str(db_path))
    store.claim("key-1", strategy_id="S", account_id="A", intent_hash="h")

    snap_path = snapshot_one(str(db_path), str(tmp_path / "backups"))
    assert Path(snap_path).exists()
    ok, detail = verify_snapshot(snap_path)
    assert ok, detail

    conn = sqlite3.connect(snap_path)
    try:
        row = conn.execute("SELECT idempotency_key FROM idempotency_records WHERE idempotency_key = 'key-1'").fetchone()
    finally:
        conn.close()
    assert row is not None


def test_snapshot_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        snapshot_one(str(tmp_path / "does_not_exist.db"), str(tmp_path / "backups"))


def test_verify_snapshot_detects_corruption(tmp_path):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a real sqlite file")
    ok, detail = verify_snapshot(str(corrupt))
    assert not ok


def test_restore_refuses_a_corrupt_snapshot(tmp_path):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a real sqlite file")
    ok, detail = restore_one(str(corrupt), str(tmp_path / "restored.db"))
    assert not ok
    assert "refusing to restore" in detail
    assert not (tmp_path / "restored.db").exists()


def test_restore_round_trips_a_valid_snapshot(tmp_path):
    db_path = tmp_path / "idem.db"
    store = SqliteIdempotencyStore(db_path=str(db_path))
    store.claim("key-1", strategy_id="S", account_id="A", intent_hash="h")
    snap_path = snapshot_one(str(db_path), str(tmp_path / "backups"))

    restore_to = tmp_path / "restored.db"
    ok, detail = restore_one(snap_path, str(restore_to))
    assert ok, detail

    restored_store = SqliteIdempotencyStore(db_path=str(restore_to))
    record = restored_store.get("key-1")
    assert record is not None


def test_backup_tooling_never_touches_a_broker_or_places_an_order():
    """Documentation-as-code: the module has no import of, or reference
    to, BrokerClient/place_order anywhere."""
    import trading.tools.backup_safety_stores as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "place_order" not in source
    assert "BrokerClient" not in source
