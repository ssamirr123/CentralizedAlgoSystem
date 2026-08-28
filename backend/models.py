"""
Compatibility re-export shim.

The StrategyHeartbeat model now lives in trading/database/models.py,
registered on the single canonical Base (Stage 2 of the architecture
consolidation). Table name ("strategy_heartbeats") and columns are
unchanged. This module only re-exports the model so existing
`from backend.models import StrategyHeartbeat` call sites keep working.
New code should import from trading.database.models directly. This file
will be removed once no importers remain.
"""
from __future__ import annotations

from trading.database.models import StrategyHeartbeat

__all__ = ["StrategyHeartbeat"]
