"""
Compatibility re-export shim.

The legacy monitoring schemas now live in trading/api/legacy.py alongside
the endpoints that use them (Stage 5). This module only re-exports them so
existing `from backend.schemas import ...` call sites keep working. New
code should import from trading.api.legacy directly. This file will be
removed once no importers remain.
"""
from __future__ import annotations

from trading.api.legacy import (
    HealthResponse,
    StrategyHeartbeatIn,
    StrategyHeartbeatOut,
    StrategyStatus,
)

__all__ = [
    "HealthResponse",
    "StrategyHeartbeatIn",
    "StrategyHeartbeatOut",
    "StrategyStatus",
]
