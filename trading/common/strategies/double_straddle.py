"""Strategy adapter for trading/algos/DoubleStraddelAlgo (Phase 10).

This class exists so DoubleStraddelAlgo can be registered in a
StrategyRegistry and queried through the common Strategy interface
(status/metrics/generate_order_intents). It deliberately does NOT import
anything from trading/algos/DoubleStraddelAlgo/ and does NOT run, start,
or otherwise touch the live algo process -- that process (main.py,
websocket_feed.py, broker/orders.py, ...) continues to run exactly as it
always has, entirely independent of this adapter.

generate_order_intents() returns an empty list -- porting the algo's real
straddle-entry/exit decision logic into this interface is explicitly
future work, not this phase's scope ("do not change trading behavior").
The already-existing Phase 3 shadow bridge
(trading/algos/DoubleStraddelAlgo/broker/execution_bridge.py) is the
current, separate mechanism that mirrors the live algo's real order
decisions into OrderIntent objects -- this adapter does not duplicate or
replace that; a future phase may make generate_order_intents() pull from
that bridge's output instead of hand-porting the decision logic again.
"""
from __future__ import annotations

from trading.common.alerts import AlertManager
from trading.common.observability import AuditTrail, MetricsRegistry
from trading.common.order_intent import OrderIntent
from trading.common.strategy import BaseStrategy
from trading.common.trading_account import ExecutionMode

STRATEGY_ID = "DoubleStraddelAlgo"


class DoubleStraddleStrategy(BaseStrategy):
    def __init__(
        self,
        *,
        execution_mode: ExecutionMode | str = ExecutionMode.SHADOW,
        metrics_registry: MetricsRegistry | None = None,
        audit_trail: AuditTrail | None = None,
        alerts: AlertManager | None = None,
    ) -> None:
        super().__init__(
            STRATEGY_ID, execution_mode=execution_mode,
            metrics_registry=metrics_registry, audit_trail=audit_trail, alerts=alerts,
        )

    def _on_generate_order_intents(self) -> list[OrderIntent]:
        return []
