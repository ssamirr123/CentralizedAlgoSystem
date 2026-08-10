import os

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "tracker.db"

# On Vercel (or any hosted env), set DATABASE_URL to a PostgreSQL connection string.
# Falls back to local SQLite for development.
DATABASE_URL: str = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")

# SQLite requires check_same_thread=False; PostgreSQL does not support it
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}


class Base(DeclarativeBase):
    pass


engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
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

