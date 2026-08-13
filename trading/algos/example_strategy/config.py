"""
Strategy-specific settings, separate from the shared runtime config in
trading/common/config.py. Add fields here as your real strategy needs
them (symbol list, quantity, risk limits, entry/exit rules, ...).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExampleStrategyConfig:
    symbol: str = field(default_factory=lambda: os.environ.get("STRATEGY_SYMBOL", "NIFTY"))
    quantity: int = field(default_factory=lambda: int(os.environ.get("STRATEGY_QUANTITY", "1")))
    loop_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("STRATEGY_LOOP_INTERVAL_SECONDS", "2"))
    )


def load_strategy_config() -> ExampleStrategyConfig:
    return ExampleStrategyConfig()
