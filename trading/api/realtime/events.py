"""Event shapes for the realtime stream.

Every event on the wire is:

    {"type": <EventType>, "seq": <int>, "ts": <iso8601>, "data": {...}}

`seq` is a process-monotonic counter -- clients dedupe on it and can
detect a gap (which just means "refetch over REST"). Control frames
(hello / ping / pong / error) are sent verbatim and carry no `seq`.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any

# --- monitoring event types (the eight the brief lists) ----------------
STRATEGY_STATUS = "strategy_status"
HEARTBEAT = "heartbeat"
PNL = "pnl"
POSITION = "position"
TRADE = "trade"
SERVER_HEALTH = "server_health"
COMMAND = "command"
ALERT = "alert"
# Stage 19 market-data engine
MARKET_QUOTE = "market_quote"
MARKET_STATUS = "market_status"

MONITORING_TYPES = frozenset(
    {STRATEGY_STATUS, HEARTBEAT, PNL, POSITION, TRADE, SERVER_HEALTH, COMMAND, ALERT,
     MARKET_QUOTE, MARKET_STATUS}
)

# --- control frame types ---------------------------------------------
HELLO = "hello"
PING = "ping"
PONG = "pong"
ERROR = "error"
SUBSCRIBED = "subscribed"

_seq = itertools.count(1)


def next_seq() -> int:
    return next(_seq)


def make_event(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    if event_type not in MONITORING_TYPES:
        raise ValueError(f"unknown monitoring event type: {event_type!r}")
    return {
        "type": event_type,
        "seq": next_seq(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
