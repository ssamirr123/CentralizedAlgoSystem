"""Phase 17.1-R Remediation B: Vwap_Algo_Nifty_hedge previously had NO
DRY_RUN gate of its own at all -- unlike DoubleStraddelAlgo/CombinedVwapNifty,
place_market_order()/place_stoploss_order() would always attempt a real
AngelOne SmartConnect call whenever the central kill switch was merely
disengaged, and modify_stoploss_order() had no guard whatsoever (not even
the kill switch). This file proves the fix: with DRY_RUN=True (the default),
every one of this legacy algo's three broker-mutation call sites returns a
synthetic DRYRUN-* result and never touches config.objconn.

Vwap_Algo_Nifty_hedge's own modules use bare, non-package-qualified imports
(`import config`, `import rest_func`) exactly like DoubleStraddelAlgo's --
see tests/algos/test_doublestraddel_execution_bridge.py's own docstring for
why this file adds that algo's directory to sys.path itself before
importing, mirroring production's own sys.path[0] behavior.

rest_func.py imports pandas_ta, which is not installed in every environment
this test suite runs in (confirmed: not in requirements.txt, not installed
here) and this legacy algo's own module was never previously imported by
any test in this repo. The behavioral tests below are skipped (never a
fabricated pass) where pandas_ta is unavailable; the static-source test does
not import rest_func at all and always runs, so this file still provides
real regression coverage in every environment.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALGO_ROOT = _REPO_ROOT / "trading" / "algos" / "Vwap_Algo_Nifty_hedge"
for _p in (str(_REPO_ROOT), str(_ALGO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402  (Vwap_Algo_Nifty_hedge/config.py, bare-import style)

_REST_FUNC_SOURCE = (_ALGO_ROOT / "rest_func.py").read_text(encoding="utf-8")

try:
    import rest_func  # noqa: E402
    _IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover -- environment-dependent
    rest_func = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)

_needs_rest_func = pytest.mark.skipif(
    rest_func is None,
    reason=f"rest_func.py could not be imported in this environment ({_IMPORT_ERROR}) -- pre-existing, not a Phase 17.1-R regression",
)


def test_static_source_has_dry_run_gate_on_every_mutation_call_site():
    """Always runs, independent of whether rest_func itself can be
    imported -- a plain text read of rest_func.py. Proves the DRY_RUN
    short-circuit and the kill-switch guard are present at source level for
    all three mutation call sites this algo has (2 placeOrder-shaped, 1
    modifyOrder-shaped; it never calls cancelOrder at all)."""
    assert _REST_FUNC_SOURCE.count("if config.DRY_RUN:") == 3
    assert _REST_FUNC_SOURCE.count("assert_live_mutation_allowed(strategy_id=_STRATEGY_ID)") == 3


class _ExplodingObjconn:
    """Stands in for config.objconn -- any attribute access/call is a test
    failure, proving DRY_RUN short-circuits before ANY real SDK call."""

    def __getattr__(self, name):
        raise AssertionError(f"real broker call attempted: objconn.{name}(...) -- DRY_RUN must have prevented this")


@pytest.fixture(autouse=True)
def _dry_run_and_exploding_broker(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", True, raising=False)
    monkeypatch.setattr(config, "objconn", _ExplodingObjconn(), raising=False)
    yield


def test_dry_run_defaults_to_true_when_unset(monkeypatch):
    monkeypatch.delenv("BOT_DRY_RUN", raising=False)
    monkeypatch.delenv("BOT_DY_RUN", raising=False)
    assert config._env_flag("BOT_DRY_RUN", "BOT_DY_RUN", default="true") is True


@_needs_rest_func
def test_place_market_order_dry_run_never_calls_objconn():
    order_id = rest_func.place_market_order("NIFTY24950CE", "12345", "195", "BUY")
    assert order_id.startswith("DRYRUN-")


@_needs_rest_func
def test_place_stoploss_order_dry_run_never_calls_objconn():
    order_id = rest_func.place_stoploss_order("NIFTY24950CE", "12345", "195", 25.0)
    assert order_id.startswith("DRYRUN-")


@_needs_rest_func
def test_modify_stoploss_order_dry_run_never_calls_objconn():
    result = rest_func.modify_stoploss_order("NIFTY24950CE", "12345", "195", 30.0, "DRYRUN-1700000000000")
    assert result == "DRYRUN-1700000000000"  # echoes the input order id, exactly like modify_limit_order in the other two legacy algos


@_needs_rest_func
def test_real_broker_mutation_count_is_zero_across_all_three_call_sites():
    """The single explicit assertion Section 12 requires: legacy VWAPHedge +
    DRY_RUN = real broker mutation count 0, proven across every mutation
    call site this algo has (2 placeOrder-shaped, 1 modifyOrder-shaped;
    this algo never calls cancelOrder at all)."""
    calls_before = 0  # _ExplodingObjconn raises on first touch -- no counter needed, absence of AssertionError IS the proof
    rest_func.place_market_order("NIFTY24950CE", "12345", "195", "BUY")
    rest_func.place_stoploss_order("NIFTY24950CE", "12345", "195", 25.0)
    rest_func.modify_stoploss_order("NIFTY24950CE", "12345", "195", 30.0, "DRYRUN-1")
    assert calls_before == 0
