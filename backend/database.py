"""
Compatibility re-export shim.

The canonical database layer is now trading/database/connection.py -- one
SQLAlchemy Base, one engine, one sessionmaker for the whole application
(Stage 2 of the architecture consolidation). This module only re-exports
those symbols so existing `from backend.database import ...` call sites
keep working. New code should import from trading.database.connection
directly. This file will be removed once no importers remain.
"""
from __future__ import annotations

from trading.database.connection import (
    DATABASE_URL,
    Base,
    SessionLocal,
    engine,
    get_db,
    init_db,
)

__all__ = ["DATABASE_URL", "Base", "SessionLocal", "engine", "get_db", "init_db"]
