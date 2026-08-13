"""Local test for trading/common/reporting.py -- mocks requests.post."""
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trading.common.reporting import report_daily_pnl, report_position, report_trade  # noqa: E402

failures = []


def check(name, condition, detail=""):
    status_ = "PASS" if condition else "FAIL"
    print(f"[{status_}] {name} {detail}")
    if not condition:
        failures.append(name)


with patch("trading.common.reporting.requests.post") as mock_post:
    mock_post.return_value = MagicMock(ok=True, status_code=200)

    ok = report_trade("http://x", "key1", "algo1", "srv1", symbol="NIFTY", side="BUY", quantity=50, price=24700.0, order_id="P-1")
    check("report_trade returns True on success", ok is True)
    call = mock_post.call_args
    check("report_trade posts to /api/trades", call.args[0] == "http://x/api/trades", call.args[0])
    check(
        "report_trade payload correct",
        call.kwargs["json"] == {
            "algo_id": "algo1", "server_id": "srv1", "symbol": "NIFTY",
            "side": "BUY", "quantity": 50, "price": 24700.0, "order_id": "P-1",
        },
        str(call.kwargs["json"]),
    )
    check("report_trade sends X-API-Key", call.kwargs["headers"] == {"X-API-Key": "key1"}, str(call.kwargs["headers"]))

with patch("trading.common.reporting.requests.post") as mock_post:
    mock_post.return_value = MagicMock(ok=True, status_code=200)
    ok = report_position("http://x", "key1", "algo1", "srv1", symbol="NIFTY", quantity=50, average_price=24700.0, last_price=24750.0, pnl=2500.0)
    check("report_position returns True on success", ok is True)
    check(
        "report_position payload correct",
        mock_post.call_args.kwargs["json"] == {
            "algo_id": "algo1", "server_id": "srv1", "symbol": "NIFTY",
            "quantity": 50, "average_price": 24700.0, "last_price": 24750.0, "pnl": 2500.0,
        },
        str(mock_post.call_args.kwargs["json"]),
    )

with patch("trading.common.reporting.requests.post") as mock_post:
    mock_post.return_value = MagicMock(ok=True, status_code=200)
    ok = report_daily_pnl("http://x", "key1", "algo1", "srv1", pnl=1500.0, trade_count=3, for_date=date(2026, 8, 12))
    check("report_daily_pnl returns True on success", ok is True)
    check(
        "report_daily_pnl payload uses pnl_date key",
        mock_post.call_args.kwargs["json"] == {
            "algo_id": "algo1", "server_id": "srv1", "pnl": 1500.0, "trade_count": 3, "pnl_date": "2026-08-12",
        },
        str(mock_post.call_args.kwargs["json"]),
    )

# --- failures are swallowed, never raise ---
with patch("trading.common.reporting.requests.post") as mock_post:
    mock_post.return_value = MagicMock(ok=False, status_code=500, text="server error")
    try:
        ok = report_trade("http://x", "key1", "algo1", "srv1", symbol="NIFTY", side="BUY", quantity=50, price=24700.0)
        check("report_trade on HTTP failure -> returns False, no exception", ok is False)
    except Exception as exc:  # noqa: BLE001
        check("report_trade on HTTP failure -> returns False, no exception", False, f"raised {exc}")

with patch("trading.common.reporting.requests.post") as mock_post:
    import requests
    mock_post.side_effect = requests.exceptions.ConnectionError("network down")
    try:
        ok = report_position("http://x", "key1", "algo1", "srv1", symbol="NIFTY", quantity=50, average_price=24700.0)
        check("report_position on network error -> returns False, no exception", ok is False)
    except Exception as exc:  # noqa: BLE001
        check("report_position on network error -> returns False, no exception", False, f"raised {exc}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
else:
    print("All checks passed.")
