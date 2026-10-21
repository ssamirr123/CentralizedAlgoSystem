"""Market-closed guards for supertrendPivotAlgo (2026-10-20 holiday incident)."""

from datetime import datetime
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "trading" / "algos" / "supertrendPivotAlgo"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ROOT))

import market_guard  # noqa: E402
import order_manager  # noqa: E402


def test_holiday_is_not_trading_day():
    assert not market_guard.is_trading_day(datetime(2026, 10, 20).date())  # Dussehra
    assert not market_guard.is_trading_day(datetime(2026, 10, 24).date())  # Saturday
    assert market_guard.is_trading_day(datetime(2026, 10, 21).date())


def test_market_open_window():
    assert not market_guard.market_is_open(datetime(2026, 10, 20, 10, 0))   # holiday
    assert not market_guard.market_is_open(datetime(2026, 10, 21, 9, 0))    # pre-open
    assert market_guard.market_is_open(datetime(2026, 10, 21, 9, 20))
    assert not market_guard.market_is_open(datetime(2026, 10, 21, 15, 45))


def test_orders_refused_when_market_closed(monkeypatch):
    class Smart:
        def placeOrder(self, params):  # pragma: no cover - must not be reached
            raise AssertionError("order sent while market closed")

    monkeypatch.setattr(order_manager.config, "DRY_RUN", False)
    monkeypatch.setattr(order_manager, "market_is_open", lambda: False)
    om = order_manager.OrderManager(Smart())
    contract = {"symbol": "NIFTY27OCT2622900CE", "token": "1", "option_type": "CE",
                "strike": 22900, "lotsize": 65}
    with pytest.raises(RuntimeError):
        om._place_order(contract, "BUY")
