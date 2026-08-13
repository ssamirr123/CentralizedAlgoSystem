import os

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "tracker.db"

# On Vercel (or any hosted env), set DATABASE_URL to a PostgreSQL connection string.
# Falls back to local SQLite for development.
DATABASE_URL: str = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")

# SQLite requires check_same_thread=False; PostgreSQL does not support it
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
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    poolclass=_poolclass,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Session:
    """Yield a transactional SQLAlchemy session for each request."""
    if DATABASE_URL.startswith("sqlite"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create required database tables if they do not already exist."""
    if DATABASE_URL.startswith("sqlite"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    from backend import models  # noqa: F401

    Base.metadata.create_all(bind=engine)

