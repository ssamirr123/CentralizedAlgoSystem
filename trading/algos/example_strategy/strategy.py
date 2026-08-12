"""
Example strategy — this is the file you replace with your real signal
logic. Everything else in trading/ (broker abstraction, logging, heartbeat,
graceful shutdown, reconnect handling) stays the same regardless of what
goes in here.

The contract main.py expects:
    - on_start(): called once after the broker connects, before the loop
    - on_tick(): called once per loop iteration; return current
      (status, mtm, day_pnl, trade_count) for heartbeat reporting
    - on_stop(): called once during shutdown, before broker.disconnect()
"""
from __future__ import annotations

import logging

from trading.algos.example_strategy.config import ExampleStrategyConfig
from trading.common.broker import BrokerClient, OrderSide, OrderType
from trading.common.logger import log_event


class ExampleStrategy:
    def __init__(self, broker: BrokerClient, config: ExampleStrategyConfig, logger: logging.Logger) -> None:
        self._broker = broker
        self._config = config
        self._logger = logger

        self.status = "RUNNING"
        self.mtm = 0.0
        self.day_pnl = 0.0
        self.trade_count = 0

    def on_start(self) -> None:
        log_event(self._logger, logging.INFO, "STRATEGY_INIT", symbol=self._config.symbol)

    def on_tick(self) -> tuple[str, float, float, int]:
        # --- Replace this block with your real signal/entry/exit logic. ---
        # Example of the pattern your real strategy would follow:
        #
        #   quote = self._broker.get_quote(self._config.symbol)
        #   if self._should_enter(quote):
        #       result = self._broker.place_order(
        #           symbol=self._config.symbol,
        #           side=OrderSide.BUY,
        #           quantity=self._config.quantity,
        #           order_type=OrderType.MARKET,
        #       )
        #       log_event(self._logger, logging.INFO, "ENTRY", order_id=result.order_id)
        #       self.trade_count += 1
        #
        quote = self._broker.get_quote(self._config.symbol)
        positions = self._broker.get_positions()
        self.mtm = sum(p.pnl for p in positions) if positions else self.mtm
        # -------------------------------------------------------------------
        return self.status, self.mtm, self.day_pnl, self.trade_count

    def on_stop(self) -> None:
        log_event(self._logger, logging.INFO, "STRATEGY_STOP", trade_count=self.trade_count)
