"""Stage 19 realtime monitoring.

An in-process async pub/sub bus (`bus`) plus a WebSocket endpoint
(`/api/ws`) that streams monitoring events to the dashboard:

    strategy_status · heartbeat · pnl · position · trade ·
    server_health · command · alert

The REST API stays the source of truth: a client fetches state over REST
on connect / reconnect, then applies the stream for liveness. Polling is
the fallback when the socket is unavailable.
"""

from trading.api.realtime.bus import bus  # noqa: F401
