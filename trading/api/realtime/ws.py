"""
WebSocket endpoint: GET /api/ws

Auth: the browser WebSocket API cannot set an Authorization header, so
the access token is passed as a subprotocol -- open with
`["cas.realtime.v1", "bearer.<jwt>"]`. A `?access_token=` query param is
accepted as a fallback. The token must be a valid, unexpired access token
for an active user holding the VIEW permission.

Frames (JSON):
    server -> client : hello | ping | pong | error | <monitoring event>
    client -> server : {"type":"ping"} | {"type":"pong"} | {"type":"subscribe","types":[...]}

Liveness: the server pings every PING_INTERVAL seconds; if it sees no
frame from the client for CLIENT_TIMEOUT seconds it closes (1001). The
client is expected to do the mirror-image and reconnect.

Backpressure: each connection has a bounded queue (bus.QUEUE_MAXSIZE). A
client that falls too far behind is closed with 1013 and expected to
reconnect + re-sync over REST.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, status
from starlette.websockets import WebSocketDisconnect, WebSocketState

from trading.api.realtime import events
from trading.api.realtime.bus import bus
from trading.api.security.permissions import Permission, permissions_for
from trading.api.security.tokens import TokenError, decode_access_token
from trading.core.config import load_settings
from trading.database import models
from trading.database.connection import SessionLocal

logger = logging.getLogger("trading.api.realtime")
router = APIRouter()

SUBPROTOCOL = "cas.realtime.v1"
_MAX_CONNECTIONS = 250
_active_connections = 0


def _extract_token(websocket: WebSocket) -> tuple[str | None, str | None]:
    """Return (token, accepted_subprotocol_or_None)."""
    offered = list(websocket.scope.get("subprotocols") or [])
    for p in offered:
        if p.startswith("bearer."):
            return p[len("bearer.") :], (SUBPROTOCOL if SUBPROTOCOL in offered else None)
    qtoken = websocket.query_params.get("access_token")
    if qtoken:
        return qtoken, (SUBPROTOCOL if SUBPROTOCOL in offered else None)
    return None, (SUBPROTOCOL if SUBPROTOCOL in offered else None)


def _authorize(token: str | None) -> tuple[int, str] | None:
    """Return (user_id, username) if the token grants VIEW, else None."""
    if not token:
        return None
    try:
        claims = decode_access_token(token)
    except TokenError:
        return None
    try:
        user_id = int(claims["sub"])
    except (KeyError, ValueError):
        return None
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.id == user_id).one_or_none()
        if user is None or not user.is_active:
            return None
        if Permission.VIEW not in permissions_for(user.role, user.extra_permissions):
            return None
        return user.id, user.username
    finally:
        db.close()


@router.websocket("/ws")
async def realtime_ws(websocket: WebSocket) -> None:
    global _active_connections
    settings = load_settings()
    ping_interval = settings.realtime_ping_interval_seconds
    client_timeout = settings.realtime_client_timeout_seconds

    token, accepted_proto = _extract_token(websocket)
    principal = _authorize(token)
    if principal is None:
        # Accept-then-close so the browser gets a clean close code rather
        # than an opaque handshake failure.
        await websocket.accept(subprotocol=accepted_proto)
        await websocket.send_json({"type": events.ERROR, "code": "unauthorized",
                                   "message": "valid VIEW access token required"})
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if _active_connections >= _MAX_CONNECTIONS:
        await websocket.accept(subprotocol=accepted_proto)
        await websocket.send_json({"type": events.ERROR, "code": "capacity",
                                   "message": "server at connection capacity, retry shortly"})
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
        return

    user_id, username = principal
    await websocket.accept(subprotocol=accepted_proto)
    _active_connections += 1
    sub = bus.subscribe()
    last_client_frame = asyncio.get_event_loop().time()
    send_lock = asyncio.Lock()

    async def send(payload: dict) -> None:
        # reader (pong) and writer (events/ping) both send -- serialise so
        # frames can never interleave on the wire.
        async with send_lock:
            await websocket.send_json(payload)

    await send({
        "type": events.HELLO,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "seq": events.next_seq(),
        "ping_interval": ping_interval,
        "client_timeout": client_timeout,
        "user": {"id": user_id, "username": username},
        "event_types": sorted(events.MONITORING_TYPES),
        "note": "REST is the source of truth; (re)sync state over REST, apply this stream for liveness",
    })
    logger.info("realtime ws open user=%s active=%d", username, _active_connections)

    async def reader() -> None:
        nonlocal last_client_frame
        while True:
            msg = await websocket.receive_json()
            last_client_frame = asyncio.get_event_loop().time()
            mtype = msg.get("type") if isinstance(msg, dict) else None
            if mtype == events.PING:
                await send({"type": events.PONG, "ts": datetime.now(timezone.utc).isoformat()})
            elif mtype == "subscribe":
                # All monitoring events are VIEW-level; ack the request but
                # the server sends everything regardless (client filters).
                await send({"type": events.SUBSCRIBED, "types": sorted(events.MONITORING_TYPES)})
            # PONG / anything else: just refreshed the liveness timer.

    async def writer() -> None:
        while True:
            if sub.dropped:
                await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
                return
            try:
                event = await asyncio.wait_for(sub.get(), timeout=ping_interval)
            except asyncio.TimeoutError:
                await send({"type": events.PING, "ts": datetime.now(timezone.utc).isoformat()})
                continue
            await send(event)

    async def liveness() -> None:
        while True:
            await asyncio.sleep(client_timeout / 2)
            idle = asyncio.get_event_loop().time() - last_client_frame
            if idle > client_timeout:
                logger.info("realtime ws idle %.0fs > %ds -> closing user=%s", idle, client_timeout, username)
                with contextlib.suppress(Exception):
                    await websocket.close(code=status.WS_1001_GOING_AWAY)
                return

    tasks = [asyncio.create_task(reader()), asyncio.create_task(writer()), asyncio.create_task(liveness())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*pending, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        sub.close()
        _active_connections -= 1
        if websocket.client_state != WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()
        logger.info("realtime ws closed user=%s active=%d", username, _active_connections)
