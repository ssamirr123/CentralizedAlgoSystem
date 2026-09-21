"""Phase 17.2-P: trading/common/portfolio_risk_store.py -- the durable
SQLite store backing PortfolioRiskManager's restart recovery. Tests the
store in isolation (schema, CRUD, corruption/missing-DB diagnosis,
concurrency) before test_portfolio_risk_restart.py exercises it wired
into a real PortfolioRiskManager end-to-end."""
from __future__ import annotations

import sqlite3
import threading
from datetime import date
from pathlib import Path

import pytest

from trading.common.portfolio_risk_store import (
    PortfolioRiskReadiness,
    PortfolioRiskStoreError,
    SqlitePortfolioRiskStore,
    open_or_diagnose,
)

TODAY = date(2026, 9, 20)


def _db(tmp_path: Path, name: str = "risk.db") -> str:
    return str(tmp_path / name)


# --------------------------------------------------------------------------- #
# Schema / initialization
# --------------------------------------------------------------------------- #
def test_first_run_creates_schema_and_marker(tmp_path):
    path = _db(tmp_path)
    store, readiness, detail = open_or_diagnose(path)
    assert readiness == PortfolioRiskReadiness.READY
    assert store is not None
    assert Path(path).is_file()
    assert Path(path + ".initialized").is_file()


def test_reopening_existing_valid_db_is_ready(tmp_path):
    path = _db(tmp_path)
    open_or_diagnose(path)  # first run
    store, readiness, detail = open_or_diagnose(path)
    assert readiness == PortfolioRiskReadiness.READY
    assert store is not None


def test_schema_version_recorded(tmp_path):
    path = _db(tmp_path)
    SqlitePortfolioRiskStore(path)
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "1"


def test_schema_version_mismatch_fails_closed(tmp_path):
    path = _db(tmp_path)
    SqlitePortfolioRiskStore(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(PortfolioRiskStoreError):
        SqlitePortfolioRiskStore(path)


# --------------------------------------------------------------------------- #
# Fail-closed diagnosis (Section 21-23, 57-58)
# --------------------------------------------------------------------------- #
def test_missing_expected_db_is_not_ready(tmp_path):
    path = _db(tmp_path)
    open_or_diagnose(path)  # first run -- creates db + marker
    Path(path).unlink()  # simulate unexpected data loss -- marker survives
    store, readiness, detail = open_or_diagnose(path)
    assert readiness == PortfolioRiskReadiness.NOT_READY
    assert store is None
    assert "missing" in detail.lower()


def test_corrupt_db_is_not_ready(tmp_path):
    path = _db(tmp_path)
    open_or_diagnose(path)
    with open(path, "wb") as f:
        f.write(b"not a real sqlite file at all, deliberately corrupted")
    store, readiness, detail = open_or_diagnose(path)
    assert readiness == PortfolioRiskReadiness.NOT_READY
    assert store is None


def test_corrupt_db_does_not_silently_recreate_empty(tmp_path):
    """The specific fail-closed behavior Section 58 asks for: a corrupted
    or unexpectedly-missing database must never be silently replaced by a
    fresh, empty one."""
    path = _db(tmp_path)
    store1, _, _ = open_or_diagnose(path)
    store1.create_reservation(
        reservation_id="r1", idempotency_key="k1", strategy_id="S", account_id="A", symbol="X",
        side="BUY", quantity=10, price=100.0, notional=1000.0, order_count_day=TODAY,
    )
    Path(path).unlink()
    store2, readiness, _ = open_or_diagnose(path)
    assert store2 is None
    assert readiness == PortfolioRiskReadiness.NOT_READY
    # The original reservation is gone (file deleted) -- but crucially,
    # nothing silently created a fresh empty replacement that a caller
    # might mistake for "everything is fine, zero outstanding risk".


# --------------------------------------------------------------------------- #
# Reservation CRUD
# --------------------------------------------------------------------------- #
def test_create_and_get_reservation(tmp_path):
    store = SqlitePortfolioRiskStore(_db(tmp_path))
    store.create_reservation(
        reservation_id="r1", idempotency_key="k1", strategy_id="S", account_id="A", symbol="X",
        side="BUY", quantity=10, price=100.0, notional=1000.0, order_count_day=TODAY,
    )
    rec = store.get("r1")
    assert rec is not None
    assert rec.status == "RESERVED"
    assert rec.strategy_id == "S"
    assert rec.notional == 1000.0
    assert rec.order_count_day == TODAY.isoformat()


def test_duplicate_reservation_id_raises(tmp_path):
    store = SqlitePortfolioRiskStore(_db(tmp_path))
    store.create_reservation(
        reservation_id="r1", idempotency_key="k1", strategy_id="S", account_id="A", symbol="X",
        side="BUY", quantity=10, price=100.0, notional=1000.0, order_count_day=TODAY,
    )
    with pytest.raises(PortfolioRiskStoreError):
        store.create_reservation(
            reservation_id="r1", idempotency_key="k2", strategy_id="S", account_id="A", symbol="Y",
            side="SELL", quantity=5, price=50.0, notional=250.0, order_count_day=TODAY,
        )


def test_transition_commits_and_is_idempotent(tmp_path):
    store = SqlitePortfolioRiskStore(_db(tmp_path))
    store.create_reservation(
        reservation_id="r1", idempotency_key="k1", strategy_id="S", account_id="A", symbol="X",
        side="BUY", quantity=10, price=100.0, notional=1000.0, order_count_day=TODAY,
    )
    ok1 = store.transition("r1", from_statuses=("RESERVED", "OPEN", "AMBIGUOUS"), to_status="COMMITTED", fill_price=101.0)
    assert ok1 is True
    ok2 = store.transition("r1", from_statuses=("RESERVED", "OPEN", "AMBIGUOUS"), to_status="COMMITTED", fill_price=101.0)
    assert ok2 is False  # already COMMITTED -- CAS refuses, no double-apply
    rec = store.get("r1")
    assert rec.status == "COMMITTED"
    assert rec.fill_price == 101.0


def test_transition_on_unknown_reservation_returns_false(tmp_path):
    store = SqlitePortfolioRiskStore(_db(tmp_path))
    assert store.transition("does-not-exist", from_statuses=("RESERVED",), to_status="RELEASED") is False


def test_list_non_terminal_excludes_committed_and_released(tmp_path):
    store = SqlitePortfolioRiskStore(_db(tmp_path))
    for i, status in enumerate(["RESERVED", "OPEN", "AMBIGUOUS", "COMMITTED", "RELEASED"]):
        rid = f"r{i}"
        store.create_reservation(
            reservation_id=rid, idempotency_key=f"k{i}", strategy_id="S", account_id="A", symbol="X",
            side="BUY", quantity=10, price=100.0, notional=1000.0, order_count_day=TODAY,
        )
        if status != "RESERVED":
            store.transition(rid, from_statuses=("RESERVED",), to_status=status)
    non_terminal = {r.reservation_id: r.status for r in store.list_non_terminal()}
    assert non_terminal == {"r0": "RESERVED", "r1": "OPEN", "r2": "AMBIGUOUS"}


def test_get_by_idempotency_key(tmp_path):
    store = SqlitePortfolioRiskStore(_db(tmp_path))
    store.create_reservation(
        reservation_id="r1", idempotency_key="the-key", strategy_id="S", account_id="A", symbol="X",
        side="BUY", quantity=10, price=100.0, notional=1000.0, order_count_day=TODAY,
    )
    rec = store.get_by_idempotency_key("the-key")
    assert rec is not None
    assert rec.reservation_id == "r1"
    assert store.get_by_idempotency_key("no-such-key") is None


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
def test_ledger_upsert_and_list(tmp_path):
    store = SqlitePortfolioRiskStore(_db(tmp_path))
    store.upsert_ledger_position(strategy_id="S", account_id="A", symbol="X", quantity=10, avg_price=100.0, realized_pnl=0.0)
    positions = store.list_ledger_positions()
    assert len(positions) == 1
    assert positions[0].quantity == 10

    store.upsert_ledger_position(strategy_id="S", account_id="A", symbol="X", quantity=20, avg_price=105.0, realized_pnl=50.0)
    positions = store.list_ledger_positions()
    assert len(positions) == 1  # updated, not duplicated
    assert positions[0].quantity == 20
    assert positions[0].realized_pnl == 50.0


# --------------------------------------------------------------------------- #
# Order counts (Section 45-47)
# --------------------------------------------------------------------------- #
def test_order_count_increment_and_read(tmp_path):
    store = SqlitePortfolioRiskStore(_db(tmp_path))
    store.adjust_order_count(scope_type="strategy", scope_id="S", trading_date=TODAY, delta=1)
    store.adjust_order_count(scope_type="strategy", scope_id="S", trading_date=TODAY, delta=1)
    assert store.get_order_count(scope_type="strategy", scope_id="S", trading_date=TODAY) == 2


def test_order_count_decrement_never_goes_negative(tmp_path):
    store = SqlitePortfolioRiskStore(_db(tmp_path))
    store.adjust_order_count(scope_type="strategy", scope_id="S", trading_date=TODAY, delta=1)
    store.adjust_order_count(scope_type="strategy", scope_id="S", trading_date=TODAY, delta=-1)
    store.adjust_order_count(scope_type="strategy", scope_id="S", trading_date=TODAY, delta=-1)
    assert store.get_order_count(scope_type="strategy", scope_id="S", trading_date=TODAY) == 0


def test_order_count_isolated_by_trading_date(tmp_path):
    store = SqlitePortfolioRiskStore(_db(tmp_path))
    day1, day2 = date(2026, 9, 20), date(2026, 9, 21)
    store.adjust_order_count(scope_type="strategy", scope_id="S", trading_date=day1, delta=3)
    store.adjust_order_count(scope_type="strategy", scope_id="S", trading_date=day2, delta=1)
    assert store.get_order_count(scope_type="strategy", scope_id="S", trading_date=day1) == 3
    assert store.get_order_count(scope_type="strategy", scope_id="S", trading_date=day2) == 1


# --------------------------------------------------------------------------- #
# Concurrency (Section 39)
# --------------------------------------------------------------------------- #
def test_concurrent_order_count_increments_are_not_lost(tmp_path):
    store = SqlitePortfolioRiskStore(_db(tmp_path))
    threads = [
        threading.Thread(target=lambda: store.adjust_order_count(scope_type="portfolio", scope_id="*", trading_date=TODAY, delta=1))
        for _ in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.get_order_count(scope_type="portfolio", scope_id="*", trading_date=TODAY) == 20


def test_concurrent_reservation_creates_all_succeed_distinct_ids(tmp_path):
    store = SqlitePortfolioRiskStore(_db(tmp_path))
    errors = []

    def _create(i):
        try:
            store.create_reservation(
                reservation_id=f"r{i}", idempotency_key=f"k{i}", strategy_id="S", account_id="A", symbol="X",
                side="BUY", quantity=1, price=1.0, notional=1.0, order_count_day=TODAY,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_create, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(store.list_non_terminal()) == 20
