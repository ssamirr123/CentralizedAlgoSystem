"""
Database engine/session for the trading control center schema.

Deliberately a SEPARATE SQLAlchemy Base from backend/database.py's (the
old simple heartbeat monitor) even though both point at the same Postgres
database via the same DATABASE_URL — this Base's create_all() only
touches the tables registered under it (servers, algos, algo_runs, ...),
never strategy_heartbeats, without needing any special-casing.

Falls back to a local SQLite file when DATABASE_URL isn't set, matching
backend/database.py's pattern, so this is testable without a real
Postgres connection.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "trading" / "data"
DB_PATH = DATA_DIR / "control_center.db"

DATABASE_URL: str = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}


class Base(DeclarativeBase):
    pass


engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)

if DATABASE_URL.startswith("sqlite"):
    # SQLite does not enforce foreign keys unless told to, per-connection.
    # Postgres (the real target) always enforces them -- without this,
    # local SQLite testing would silently accept FK violations that
    # production would reject, masking real bugs.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Session:
    """Yield a transactional SQLAlchemy session for each request/operation."""
    if DATABASE_URL.startswith("sqlite"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create this schema's tables if they don't already exist. Does not
    touch backend/models.py's strategy_heartbeats table -- separate Base."""
    if DATABASE_URL.startswith("sqlite"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    from trading.database import models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(bind=engine)
