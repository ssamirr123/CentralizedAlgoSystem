"""
StrategyRegistry -- holds every registered Strategy by strategy_id and
exposes lifecycle control (enable/disable/start/stop) and read access
(status/metrics/order-intent generation) without callers ever touching a
concrete strategy class directly.

    Strategy -> StrategyRegistry -> StrategyAssignment -> TradingAccount
        -> BrokerManager -> BrokerAdapter

StrategyRegistry deliberately does NOT replace StrategyAssignment (Phase
1/7's strategy_id -> account_id/execution_mode/risk_profile mapping in
trading/common/strategy_assignment.py) -- the two are complementary and
orthogonal:

  - StrategyRegistry answers "what strategies exist, and what is each
    one's own lifecycle status/metrics/generated intents?"
  - StrategyAssignment answers "which TradingAccount is a given strategy_id
    currently routed to, and in what execution_mode?"

Neither needs to know about the other's internals. A caller wiring the
full pipeline holds both: look a strategy up in StrategyRegistry to pull
its OrderIntents, then hand each one to RiskManager/ExecutionEngine, which
already consults StrategyAssignment itself.

This registry never calls a broker, never imports anything under
trading.common.brokers, and never places or has any bearing on whether a
real order is placed -- it only manages Strategy objects' own in-memory
lifecycle state.
"""
from __future__ import annotations

from trading.common.order_intent import OrderIntent
from trading.common.strategy import Strategy, StrategyMetrics, StrategyStatus


class UnknownStrategyError(KeyError):
    """Raised when a strategy_id has no registered Strategy."""


class DuplicateStrategyError(ValueError):
    """Raised when register() is called with a strategy_id already registered."""


class StrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[str, Strategy] = {}

    # -- registration ---------------------------------------------------------- #
    def register(self, strategy: Strategy) -> None:
        if strategy.strategy_id in self._strategies:
            raise DuplicateStrategyError(f"Strategy '{strategy.strategy_id}' is already registered.")
        strategy.initialize()
        self._strategies[strategy.strategy_id] = strategy

    def unregister(self, strategy_id: str) -> None:
        self._strategies.pop(strategy_id, None)

    def get(self, strategy_id: str) -> Strategy:
        try:
            return self._strategies[strategy_id]
        except KeyError:
            raise UnknownStrategyError(f"No strategy registered under '{strategy_id}'.") from None

    def is_registered(self, strategy_id: str) -> bool:
        return strategy_id in self._strategies

    def strategy_ids(self) -> list[str]:
        return list(self._strategies.keys())

    def strategies(self) -> list[Strategy]:
        return list(self._strategies.values())

    # -- lifecycle control ------------------------------------------------------ #
    def enable(self, strategy_id: str) -> None:
        self.get(strategy_id).enable()

    def disable(self, strategy_id: str) -> None:
        self.get(strategy_id).disable()

    def start(self, strategy_id: str) -> None:
        self.get(strategy_id).start()

    def stop(self, strategy_id: str) -> None:
        self.get(strategy_id).stop()

    def start_all(self) -> None:
        """Best-effort: starts every currently ENABLED strategy. A strategy
        not yet enabled (or already active/stopped/errored) is left alone
        -- callers that need "enable everything then start everything" call
        enable() explicitly first, since silently enabling on someone's
        behalf here would hide a configuration mistake."""
        for strategy in self._strategies.values():
            if strategy.get_status() == StrategyStatus.ENABLED:
                strategy.start()

    def stop_all(self) -> None:
        for strategy in self._strategies.values():
            if strategy.get_status() in (StrategyStatus.RUNNING, StrategyStatus.SHADOW):
                strategy.stop()

    # -- read access -------------------------------------------------------------- #
    def get_status(self, strategy_id: str) -> StrategyStatus:
        return self.get(strategy_id).get_status()

    def get_metrics(self, strategy_id: str) -> StrategyMetrics:
        return self.get(strategy_id).get_metrics()

    def get_all_statuses(self) -> dict[str, StrategyStatus]:
        return {sid: s.get_status() for sid, s in self._strategies.items()}

    def get_all_metrics(self) -> dict[str, StrategyMetrics]:
        return {sid: s.get_metrics() for sid, s in self._strategies.items()}

    # -- order-intent generation ---------------------------------------------------- #
    def generate_order_intents(self, strategy_id: str) -> list[OrderIntent]:
        return self.get(strategy_id).generate_order_intents()

    def generate_all_order_intents(self) -> dict[str, list[OrderIntent]]:
        """Only polls strategies currently RUNNING or SHADOW -- a
        DISABLED/STOPPED/ERROR strategy is silently skipped (not an error),
        matching start_all()'s "best effort" philosophy."""
        out: dict[str, list[OrderIntent]] = {}
        for strategy_id, strategy in self._strategies.items():
            if strategy.get_status() in (StrategyStatus.RUNNING, StrategyStatus.SHADOW):
                out[strategy_id] = strategy.generate_order_intents()
        return out
