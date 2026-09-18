"""Phase 10: the three concrete strategy adapters registered for
DoubleStraddelAlgo, CombinedVwapNifty, and Vwap_Algo_Nifty_hedge.

Safety focus of this file: none of these adapters may import anything
from their corresponding trading/algos/<Name>/ directory (that would risk
the exact real-credential-leak-at-import-time hazard Phase 5A already hit
and fixed for DoubleStraddelAlgo/config.py), and none may generate a
non-empty OrderIntent list yet (Phase 10's explicit scope is the registry
skeleton, not real decision-logic integration -- "do not change trading
behavior").
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from trading.common.order_intent import OrderIntent
from trading.common.strategies.combined_vwap_nifty import STRATEGY_ID as VWAP_ID
from trading.common.strategies.combined_vwap_nifty import CombinedVwapNiftyStrategy
from trading.common.strategies.double_straddle import STRATEGY_ID as DOUBLE_STRADDLE_ID
from trading.common.strategies.double_straddle import DoubleStraddleStrategy
from trading.common.strategies.vwap_algo_nifty_hedge import STRATEGY_ID as HEDGE_ID
from trading.common.strategies.vwap_algo_nifty_hedge import VwapAlgoNiftyHedgeStrategy
from trading.common.strategy import Strategy, StrategyStatus
from trading.common.trading_account import ExecutionMode

_ADAPTERS = pytest.mark.parametrize(
    "adapter_cls,expected_id",
    [
        (DoubleStraddleStrategy, DOUBLE_STRADDLE_ID),
        (CombinedVwapNiftyStrategy, VWAP_ID),
        (VwapAlgoNiftyHedgeStrategy, HEDGE_ID),
    ],
    ids=["DoubleStraddelAlgo", "CombinedVwapNifty", "Vwap_Algo_Nifty_hedge"],
)


def test_strategy_ids_match_the_three_named_algos():
    assert DOUBLE_STRADDLE_ID == "DoubleStraddelAlgo"
    assert VWAP_ID == "CombinedVwapNifty"
    assert HEDGE_ID == "Vwap_Algo_Nifty_hedge"


@_ADAPTERS
def test_adapter_implements_strategy(adapter_cls, expected_id):
    strategy = adapter_cls()
    assert isinstance(strategy, Strategy)
    assert strategy.strategy_id == expected_id


@_ADAPTERS
def test_adapter_defaults_to_shadow_execution_mode(adapter_cls, expected_id):
    strategy = adapter_cls()
    assert strategy.execution_mode == ExecutionMode.SHADOW


@_ADAPTERS
def test_adapter_full_lifecycle_reaches_shadow_then_stopped(adapter_cls, expected_id):
    strategy = adapter_cls()
    strategy.initialize()
    strategy.enable()
    strategy.start()
    assert strategy.get_status() == StrategyStatus.SHADOW
    strategy.stop()
    assert strategy.get_status() == StrategyStatus.STOPPED


@_ADAPTERS
def test_adapter_generate_order_intents_returns_an_empty_list_today(adapter_cls, expected_id):
    """Documented, deliberate: real decision-logic integration is future
    work -- see each adapter module's own docstring."""
    strategy = adapter_cls()
    strategy.enable()
    strategy.start()

    intents = strategy.generate_order_intents()

    assert intents == []
    assert isinstance(intents, list)


@_ADAPTERS
def test_adapter_can_run_in_paper_mode_instead_of_shadow(adapter_cls, expected_id):
    strategy = adapter_cls(execution_mode=ExecutionMode.PAPER)
    strategy.enable()
    strategy.start()
    assert strategy.get_status() == StrategyStatus.RUNNING


# --------------------------------------------------------------------------- #
# Safety: none of these adapters import anything under trading.algos.*
# --------------------------------------------------------------------------- #
_ADAPTER_FILES = [
    "trading/common/strategies/double_straddle.py",
    "trading/common/strategies/combined_vwap_nifty.py",
    "trading/common/strategies/vwap_algo_nifty_hedge.py",
]


@pytest.mark.parametrize("relative_path", _ADAPTER_FILES)
def test_adapter_source_never_imports_a_live_algo_module(relative_path):
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    source = (repo_root / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module] if node.module else []
        else:
            continue
        for name in names:
            assert name is None or "trading.algos" not in name, (
                f"{relative_path} imports {name!r} -- adapters must not import live algo code"
            )


def test_registering_all_three_adapters_never_touches_trading_algos_modules():
    """Import-time + full-lifecycle proof: sys.modules gains no
    trading.algos.* entry as a side effect of using these adapters."""
    import sys

    before = {name for name in sys.modules if name.startswith("trading.algos")}

    for adapter_cls in (DoubleStraddleStrategy, CombinedVwapNiftyStrategy, VwapAlgoNiftyHedgeStrategy):
        strategy = adapter_cls()
        strategy.initialize()
        strategy.enable()
        strategy.start()
        strategy.generate_order_intents()
        strategy.stop()

    after = {name for name in sys.modules if name.startswith("trading.algos")}
    assert after == before
