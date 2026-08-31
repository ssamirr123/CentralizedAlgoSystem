"""
Alembic migration environment.

Single source of truth for the schema: trading.database.connection.Base.
Every model in trading.database.models is imported below so the full
metadata (all 11 tables) is populated before autogenerate compares it
against the database.

The engine and URL come from trading.database.connection -- the same
engine the application uses (NullPool, SQLite FK pragma via an on-connect
event listener, connect_args). The URL is DATABASE_URL from the
environment (PostgreSQL in production), falling back to a local SQLite
file only when unset (isolated tests).
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Make the project root importable when alembic is invoked from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Canonical engine + Base, and every model registered on that Base.
from trading.database.connection import DATABASE_URL, Base, engine  # noqa: E402
from trading.database import models  # noqa: E402,F401  (registers all tables)

config = context.config

# alembic.ini leaves sqlalchemy.url blank on purpose; surface the real URL
# for offline mode and for `alembic` status output.
config.set_main_option("sqlalchemy.url", DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_RENDER_AS_BATCH = DATABASE_URL.startswith("sqlite")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=_RENDER_AS_BATCH,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database using the application engine."""
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=_RENDER_AS_BATCH,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
