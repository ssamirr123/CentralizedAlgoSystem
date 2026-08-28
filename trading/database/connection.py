"""
Canonical database engine/session/Base for the whole application.

This is the single SQLAlchemy DeclarativeBase for the project. Every model
lives under trading/database/models.py and registers on this Base's
metadata -- the control-center tables (servers, algos, algo_runs, ...)
AND the legacy strategy_heartbeats table (moved here in Stage 2 of the
architecture consolidation; backend/database.py and backend/models.py are
now thin re-export shims pointing here).

PostgreSQL (via DATABASE_URL) is the authoritative production database.
Falls back to a local SQLite file only when DATABASE_URL isn't set, so the
suite is runnable without a real Postgres connection.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "trading" / "data"
DB_PATH = DATA_DIR / "control_center.db"

DATABASE_URL: str = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

# NullPool on Postgres: serverless instances are short-lived and each one
# creates its own engine, so SQLAlchemy's default in-process pool (up to
# pool_size + max_overflow = 15 real connections per engine) multiplies
# across concurrent invocations and blows past Supabase's session-pooler
# cap (15 total). Supabase's own pooler already does the multiplexing.
_poolclass = NullPool if not DATABASE_URL.startswith("sqlite") else None


class Base(DeclarativeBase):
    pass


engine = create_engine(
    DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True, poolclass=_poolclass
)

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
    """Create every registered table if it does not already exist, including
    the legacy strategy_heartbeats table (single canonical Base since Stage
    2). Idempotent: create_all() checks first and never drops or alters an
    existing table. This is a convenience for local/dev and tests; Alembic
    is the migration mechanism for real deployments (Stage 3)."""
    if DATABASE_URL.startswith("sqlite"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    from trading.database import models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(bind=engine)
