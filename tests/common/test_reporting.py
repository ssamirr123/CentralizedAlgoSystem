"""trading/common/reporting.py -- ported from
trading/common/test_reporting_local.py. requests.post is mocked."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import requests

from trading.common.reporting import report_daily_pnl, report_position, report_trade


def test_report_trade_success():
    with patch("trading.common.reporting.requests.post") as mock_post:
        mock_post.return_value = MagicMock(ok=True, status_code=200)
        ok = report_trade("http://x", "key1", "algo1", "srv1",
                          symbol="NIFTY", side="BUY", quantity=50, price=24700.0, order_id="P-1")
        assert ok is True
        call = mock_post.call_args
        assert call.args[0] == "http://x/api/trades"
        assert call.kwargs["json"] == {
            "algo_id": "algo1", "server_id": "srv1", "symbol": "NIFTY",
            "side": "BUY", "quantity": 50, "price": 24700.0, "order_id": "P-1",
        }
        assert call.kwargs["headers"] == {"X-API-Key": "key1"}


def test_report_position_success():
    with patch("trading.common.reporting.requests.post") as mock_post:
        mock_post.return_value = MagicMock(ok=True, status_code=200)
        ok = report_position("http://x", "key1", "algo1", "srv1",
                             symbol="NIFTY", quantity=50, average_price=24700.0,
                             last_price=24750.0, pnl=2500.0)
        assert ok is True
        assert mock_post.call_args.kwargs["json"] == {
            "algo_id": "algo1", "server_id": "srv1", "symbol": "NIFTY",
            "quantity": 50, "average_price": 24700.0, "last_price": 24750.0, "pnl": 2500.0,
        }


def test_report_daily_pnl_uses_pnl_date_key():
    with patch("trading.common.reporting.requests.post") as mock_post:
        mock_post.return_value = MagicMock(ok=True, status_code=200)
        ok = report_daily_pnl("http://x", "key1", "algo1", "srv1",
                              pnl=1500.0, trade_count=3, for_date=date(2026, 8, 12))
        assert ok is True
        assert mock_post.call_args.kwargs["json"] == {
            "algo_id": "algo1", "server_id": "srv1", "pnl": 1500.0,
            "trade_count": 3, "pnl_date": "2026-08-12",
        }


def test_http_failure_returns_false_no_raise():
    with patch("trading.common.reporting.requests.post") as mock_post:
        mock_post.return_value = MagicMock(ok=False, status_code=500, text="server error")
        assert report_trade("http://x", "k", "a", "s",
                            symbol="NIFTY", side="BUY", quantity=1, price=1.0) is False


def test_network_error_returns_false_no_raise():
    with patch("trading.common.reporting.requests.post") as mock_post:
        mock_post.side_effect = requests.exceptions.ConnectionError("network down")
        assert report_position("http://x", "k", "a", "s",
                               symbol="NIFTY", quantity=1, average_price=1.0) is False
