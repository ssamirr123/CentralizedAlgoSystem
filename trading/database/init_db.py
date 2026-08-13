"""
One-shot script: creates this schema's tables (servers, algos, algo_runs,
heartbeats, logs, positions, trades, daily_pnl, commands) against
whatever DATABASE_URL points at. Safe to re-run -- create_all() only
creates tables that don't already exist, and never touches
strategy_heartbeats (that's backend/models.py's separate Base).

Usage:
    python trading/database/init_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from trading.database.connection import DATABASE_URL, init_db


def main() -> int:
    target = "SQLite (local)" if DATABASE_URL.startswith("sqlite") else "configured DATABASE_URL"
    print(f"Creating tables against: {target}")
    init_db()
    print("Done. Tables: servers, algos, algo_runs, heartbeats, logs, positions, trades, daily_pnl, commands")
    return 0


if __name__ == "__main__":
    sys.exit(main())
