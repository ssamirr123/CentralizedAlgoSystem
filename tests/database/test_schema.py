"""Canonical single-Base schema checks."""
from __future__ import annotations

from trading.database import models as tmodels
from trading.database.connection import Base

_EXPECTED_TABLES = {
    "servers", "algos", "algo_runs", "heartbeats", "logs", "positions",
    "trades", "daily_pnl", "commands", "rate_limit_windows", "strategy_heartbeats",
    "users", "auth_sessions", "audit_log",
}


def test_all_tables_on_one_base():
    assert _EXPECTED_TABLES.issubset(set(Base.metadata.tables))


def test_strategy_heartbeats_table_unchanged():
    t = tmodels.StrategyHeartbeat.__table__
    assert t.name == "strategy_heartbeats"
    cols = {c.name for c in t.columns}
    assert cols == {
        "id", "strategy_name", "server_name", "status", "current_mtm",
        "day_pnl", "number_of_trades", "last_update_time", "received_at",
    }
    uniques = [
        tuple(sorted(c.name for c in con.columns))
        for con in t.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    ]
    assert ("server_name", "strategy_name") in uniques


def test_create_all_builds_every_table(db_engine):
    from sqlalchemy import inspect

    names = set(inspect(db_engine).get_table_names())
    assert _EXPECTED_TABLES.issubset(names)
