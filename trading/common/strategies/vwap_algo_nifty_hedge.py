"""Strategy adapter for trading/algos/Vwap_Algo_Nifty_hedge (Phase 10).

See trading/common/strategies/double_straddle.py's module docstring for
the full rationale -- this adapter follows the identical pattern: no
import from trading/algos/Vwap_Algo_Nifty_hedge/, no interaction with that
algo's live process, generate_order_intents() returns an empty list
because porting its real hedge entry/exit decision logic into this
interface is explicitly deferred to a future phase.
"""
from __future__ import annotations

from trading.common.alerts import AlertManager
from trading.common.observability import AuditTrail, MetricsRegistry
from trading.common.order_intent import OrderIntent
from trading.common.strategy import BaseStrategy
from trading.common.trading_account import ExecutionMode

STRATEGY_ID = "Vwap_Algo_Nifty_hedge"


class VwapAlgoNiftyHedgeStrategy(BaseStrategy):
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
