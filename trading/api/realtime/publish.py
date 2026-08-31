"""Typed publish helpers, called from the REST handlers + the watcher.

Every helper is best-effort: a realtime-publish failure is logged and
swallowed so it can never affect the API response or the underlying DB
write. No trading-strategy code imports this module.
"""
from __future__ import annotations

import logging
from typing import Any

from trading.api.realtime import events
from trading.api.realtime.bus import bus

logger = logging.getLogger("trading.api.realtime")


def _emit(event_type: str, data: dict[str, Any]) -> None:
    try:
        bus.publish(events.make_event(event_type, data))
    except Exception:  # noqa: BLE001 -- realtime must never break a request
        logger.exception("realtime publish failed: type=%s", event_type)


def heartbeat(algo_id: str, server_id: str, *, status: str, cpu: float | None = None,
              memory: float | None = None, pnl: float | None = None,
              position: str | None = None, timestamp: str | None = None) -> None:
    _emit(events.HEARTBEAT, {
        "algo_id": algo_id, "server_id": server_id, "status": status,
        "cpu": cpu, "memory": memory, "pnl": pnl, "position": position,
        "timestamp": timestamp,
    })


def strategy_status(algo_id: str, server_id: str, *, status: str,
                    previous_status: str | None = None, source: str = "heartbeat") -> None:
    _emit(events.STRATEGY_STATUS, {
        "algo_id": algo_id, "server_id": server_id, "status": status,
        "previous_status": previous_status, "source": source,
    })


def pnl(algo_id: str, server_id: str, *, date: str, pnl: float, trade_count: int) -> None:
    _emit(events.PNL, {
        "algo_id": algo_id, "server_id": server_id, "date": date,
        "pnl": pnl, "trade_count": trade_count,
    })


def position(algo_id: str, server_id: str, *, symbol: str, quantity: int,
             average_price: float | None = None, last_price: float | None = None,
             pnl: float | None = None, closed: bool = False) -> None:
    _emit(events.POSITION, {
        "algo_id": algo_id, "server_id": server_id, "symbol": symbol,
        "quantity": quantity, "average_price": average_price,
        "last_price": last_price, "pnl": pnl, "closed": closed,
    })


def trade(algo_id: str, server_id: str, *, symbol: str, side: str, quantity: int,
          price: float, executed_at: str | None = None, order_id: str | None = None) -> None:
    _emit(events.TRADE, {
        "algo_id": algo_id, "server_id": server_id, "symbol": symbol, "side": side,
        "quantity": quantity, "price": price, "executed_at": executed_at, "order_id": order_id,
    })


def server_health(server_id: str, *, status: str, ssm_status: str | None = None,
                  healthy: bool | None = None, last_heartbeat: str | None = None,
                  source: str = "api") -> None:
    _emit(events.SERVER_HEALTH, {
        "server_id": server_id, "status": status, "ssm_status": ssm_status,
        "healthy": healthy, "last_heartbeat": last_heartbeat, "source": source,
    })


def command(*, command_id: int | None, algo_id: str | None, server_id: str,
            action: str, status: str, job_id: str | None = None,
            requested_by: str | None = None, message: str | None = None) -> None:
    _emit(events.COMMAND, {
        "command_id": command_id, "algo_id": algo_id, "server_id": server_id,
        "action": action, "status": status, "job_id": job_id,
        "requested_by": requested_by, "message": message,
    })


def alert(*, kind: str, severity: str, message: str, algo_id: str | None = None,
          server_id: str | None = None, detail: dict | None = None) -> None:
    _emit(events.ALERT, {
        "kind": kind, "severity": severity, "message": message,
        "algo_id": algo_id, "server_id": server_id, "detail": detail or {},
    })
