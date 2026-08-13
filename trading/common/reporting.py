"""
Reports trades/positions/daily P&L to the control-center API
(POST /api/trades, /api/positions, /api/pnl).

Unlike the heartbeat/log senders (trading/common/heartbeat.py,
log_shipper.py), this is NOT a background thread -- trades and position
changes are discrete events tied directly to an actual broker order fill,
not periodic ticks, so they're reported synchronously right after the
fill. Deliberately single-attempt with a short timeout and no retry: a
failed report must never block or delay the strategy loop -- trading
logic always takes priority over dashboard reporting. Failures are logged
and swallowed, never raised.
"""
from __future__ import annotations

import logging
from datetime import date as date_type
from datetime import datetime
from typing import Any

import requests

_logger = logging.getLogger("trading.reporting")

_REQUEST_TIMEOUT_SECONDS = 5


def _post(api_base_url: str, api_key: str, path: str, payload: dict[str, Any]) -> bool:
    try:
        response = requests.post(
            f"{api_base_url.rstrip('/')}{path}",
            json=payload,
            headers={"X-API-Key": api_key},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if response.ok:
            return True
        _logger.error("Report to %s rejected: status=%s body=%s", path, response.status_code, response.text)
    except requests.RequestException as exc:
        _logger.error("Report to %s failed: %s", path, exc)
    return False


def report_trade(
    api_base_url: str,
    api_key: str,
    algo_name: str,
    server_name: str,
    symbol: str,
    side: str,
    quantity: int,
    price: float,
    order_id: str | None = None,
    executed_at: datetime | None = None,
) -> bool:
    payload = {
        "algo_id": algo_name, "server_id": server_name, "symbol": symbol,
        "side": side, "quantity": quantity, "price": price, "order_id": order_id,
    }
    if executed_at is not None:
        payload["executed_at"] = executed_at.isoformat()
    return _post(api_base_url, api_key, "/api/trades", payload)


def report_position(
    api_base_url: str,
    api_key: str,
    algo_name: str,
    server_name: str,
    symbol: str,
    quantity: int,
    average_price: float,
    last_price: float | None = None,
    pnl: float | None = None,
) -> bool:
    """quantity=0 closes the position (deletes the row server-side) --
    call this after a position is fully squared off, not just skip it."""
    payload = {
        "algo_id": algo_name, "server_id": server_name, "symbol": symbol,
        "quantity": quantity, "average_price": average_price,
        "last_price": last_price, "pnl": pnl,
    }
    return _post(api_base_url, api_key, "/api/positions", payload)


def report_daily_pnl(
    api_base_url: str,
    api_key: str,
    algo_name: str,
    server_name: str,
    pnl: float,
    trade_count: int = 0,
    for_date: date_type | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "algo_id": algo_name, "server_id": server_name, "pnl": pnl, "trade_count": trade_count,
    }
    if for_date is not None:
        payload["pnl_date"] = for_date.isoformat()
    return _post(api_base_url, api_key, "/api/pnl", payload)
