"""
Control-center API routes. Mounted onto the existing backend/main.py
FastAPI app under /api (see backend/main.py's app.include_router call).

Algo control actions (start/stop/restart/update) are async, matching
Milestone 4's Lambda design directly: create a Command audit row, invoke
the Lambda, store the returned job_id, return immediately. The caller
polls GET /api/command/{command_id} for the real outcome -- this endpoint
never claims RUNNING just because the Lambda accepted the request.

GET endpoints (server/status, logs, pnl, positions) read straight from
Supabase, no Lambda call -- logs/pnl/positions will be empty until
Milestones 8-10 wire up ingestion, which is expected at this milestone.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from trading.api.deps import get_db, require_api_key
from trading.api.lambda_client import LambdaInvokeError, invoke_orchestrator
from trading.api.schemas import (
    AlgoActionRequest,
    AlgoListEntry,
    AlgoStatusResponse,
    CommandResponse,
    DailyPnlEntry,
    DailyPnlIn,
    HeartbeatAck,
    HeartbeatIn,
    LogAck,
    LogEntry,
    LogIn,
    PositionAck,
    PositionEntry,
    PositionIn,
    ServerListEntry,
    ServerStatusResponse,
    TradeAck,
    TradeEntry,
    TradeIn,
)
from trading.database import models

router = APIRouter(dependencies=[Depends(require_api_key)])

_ACTION_TO_AGENT_COMMAND = {
    "start": "START_ALGO",
    "stop": "STOP_ALGO",
    "restart": "RESTART_ALGO",
    "update": "UPDATE",
}


def _resolve_server(db: Session, server_name: str) -> models.Server:
    server = db.query(models.Server).filter(models.Server.name == server_name).one_or_none()
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown server: {server_name}")
    return server


def _get_or_create_algo(db: Session, algo_name: str, server: models.Server) -> models.Algo:
    algo = (
        db.query(models.Algo)
        .filter(models.Algo.name == algo_name, models.Algo.server_id == server.id)
        .one_or_none()
    )
    if algo is not None:
        return algo
    algo = models.Algo(
        name=algo_name,
        server_id=server.id,
        script_path=f"trading/algos/{algo_name}/main.py",
    )
    db.add(algo)
    db.flush()  # assigns algo.id without committing yet
    return algo


def _run_algo_action(action: str, body: AlgoActionRequest, db: Session) -> CommandResponse:
    server = _resolve_server(db, body.server_id)
    algo = _get_or_create_algo(db, body.algo_id, server)

    command_row = models.Command(
        algo_id=algo.id,
        server_id=server.id,
        command=_ACTION_TO_AGENT_COMMAND[action],
        requested_by=body.requested_by,
        status="PENDING",
    )
    db.add(command_row)
    db.commit()
    db.refresh(command_row)

    try:
        lambda_action = {"start": "start_algo", "stop": "stop_algo", "restart": "restart_algo", "update": "update_algo"}[action]
        result = invoke_orchestrator(lambda_action, algo_id=body.algo_id)
    except LambdaInvokeError as exc:
        command_row.status = "FAILED"
        command_row.error = str(exc)
        db.commit()
        return CommandResponse(success=False, command_id=command_row.id, status="FAILED", message=str(exc))

    command_row.job_id = result.get("job_id")
    command_row.status = result.get("status", "FAILED")
    command_row.result = result
    if not result.get("success"):
        command_row.error = result.get("error")
    db.commit()

    return CommandResponse(
        success=bool(result.get("success")),
        command_id=command_row.id,
        job_id=result.get("job_id"),
        status=result.get("status", "UNKNOWN"),
        message=result.get("error") or result.get("message"),
    )


@router.post("/algo/start", response_model=CommandResponse)
def start_algo(body: AlgoActionRequest, db: Session = Depends(get_db)) -> CommandResponse:
    return _run_algo_action("start", body, db)


@router.post("/algo/stop", response_model=CommandResponse)
def stop_algo(body: AlgoActionRequest, db: Session = Depends(get_db)) -> CommandResponse:
    return _run_algo_action("stop", body, db)


@router.post("/algo/restart", response_model=CommandResponse)
def restart_algo(body: AlgoActionRequest, db: Session = Depends(get_db)) -> CommandResponse:
    return _run_algo_action("restart", body, db)


@router.post("/algo/update", response_model=CommandResponse)
def update_algo(body: AlgoActionRequest, db: Session = Depends(get_db)) -> CommandResponse:
    return _run_algo_action("update", body, db)


@router.get("/command/{command_id}", response_model=CommandResponse)
def get_command(command_id: int, db: Session = Depends(get_db)) -> CommandResponse:
    command_row = db.query(models.Command).filter(models.Command.id == command_id).one_or_none()
    if command_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown command_id: {command_id}")

    if command_row.status in ("PENDING", "STARTING", "STOPPING", "RESTARTING", "UPDATING") and command_row.job_id:
        try:
            result = invoke_orchestrator("get_command_status", job_id=command_row.job_id)
        except LambdaInvokeError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not reach Lambda: {exc}") from exc

        if result.get("status") != "IN_PROGRESS":
            command_row.status = result.get("status", command_row.status)
            command_row.result = result
            if not result.get("success"):
                command_row.error = result.get("error") or result.get("message")
            db.commit()

    return CommandResponse(
        success=command_row.status not in ("FAILED", "ERROR", "UNKNOWN"),
        command_id=command_row.id,
        job_id=command_row.job_id,
        status=command_row.status,
        message=command_row.error,
    )


@router.get("/algo/status", response_model=AlgoStatusResponse)
def algo_status(algo_id: str, server_id: str) -> AlgoStatusResponse:
    try:
        result = invoke_orchestrator("get_algo_status", algo_id=algo_id)
    except LambdaInvokeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not reach Lambda: {exc}") from exc

    return AlgoStatusResponse(
        success=bool(result.get("success")),
        algo_id=algo_id,
        status=result.get("status", "UNKNOWN"),
        pid=result.get("pid"),
        started_at=result.get("started_at"),
        message=result.get("error") or result.get("message"),
    )


@router.get("/server/status", response_model=ServerStatusResponse)
def server_status(server_id: str, db: Session = Depends(get_db)) -> ServerStatusResponse:
    server = _resolve_server(db, server_id)
    return ServerStatusResponse(
        name=server.name,
        ec2_instance_id=server.ec2_instance_id,
        region=server.region,
        status=server.status,
        last_heartbeat=server.last_heartbeat,
    )


@router.get("/servers", response_model=list[ServerListEntry])
def list_servers(db: Session = Depends(get_db)) -> list[ServerListEntry]:
    rows = db.query(models.Server).order_by(models.Server.name).all()
    return [
        ServerListEntry(
            server_id=r.name, ec2_instance_id=r.ec2_instance_id, region=r.region,
            status=r.status, last_heartbeat=r.last_heartbeat,
        )
        for r in rows
    ]


@router.get("/algos", response_model=list[AlgoListEntry])
def list_algos(db: Session = Depends(get_db)) -> list[AlgoListEntry]:
    # algos has no last_heartbeat column (matches the schema's original
    # field list) -- computed here via MAX(timestamp) per algo instead of
    # requiring an ALTER TABLE on an already-created Supabase table.
    latest_hb = (
        db.query(models.Heartbeat.algo_id, func.max(models.Heartbeat.timestamp).label("last_heartbeat"))
        .group_by(models.Heartbeat.algo_id)
        .subquery()
    )

    rows = (
        db.query(models.Algo, latest_hb.c.last_heartbeat)
        .join(models.Server)
        .outerjoin(latest_hb, latest_hb.c.algo_id == models.Algo.id)
        .order_by(models.Server.name, models.Algo.name)
        .all()
    )
    return [
        AlgoListEntry(
            algo_id=algo.name, server_id=algo.server.name, status=algo.status,
            enabled=algo.enabled, script_path=algo.script_path, updated_at=algo.updated_at,
            last_heartbeat=last_heartbeat,
        )
        for algo, last_heartbeat in rows
    ]


@router.post("/heartbeat", response_model=HeartbeatAck)
def post_heartbeat(body: HeartbeatIn, db: Session = Depends(get_db)) -> HeartbeatAck:
    """Appends to heartbeats history (unlike the old strategy_heartbeats
    table, this is a log, not an upsert-one-row-per-pair table) and
    updates algos.status + servers.last_heartbeat for fast list-view
    reads. Deliberately does NOT touch servers.status -- that's EC2 power
    state, a separate concern from an individual algo's health."""
    server = _resolve_server(db, body.server_id)
    algo = _get_or_create_algo(db, body.algo_id, server)

    ts = body.timestamp or datetime.now(timezone.utc)

    db.add(models.Heartbeat(
        algo_id=algo.id, server_id=server.id, timestamp=ts, status=body.status,
        cpu=body.cpu, memory=body.memory, pnl=body.pnl, position=body.position,
    ))
    algo.status = body.status
    server.last_heartbeat = ts
    db.commit()

    return HeartbeatAck(success=True, algo_id=body.algo_id, server_id=body.server_id)


@router.get("/logs", response_model=list[LogEntry])
def get_logs(
    algo_id: str,
    server_id: str,
    limit: int = 100,
    level: str | None = None,
    event: str | None = None,
    log_date: str | None = None,  # YYYY-MM-DD, matches that calendar day (UTC)
    db: Session = Depends(get_db),
) -> list[LogEntry]:
    server = _resolve_server(db, server_id)
    algo = _get_or_create_algo(db, algo_id, server)
    db.commit()

    query = db.query(models.Log).filter(models.Log.algo_id == algo.id)
    if level:
        query = query.filter(models.Log.level == level.upper())
    if event:
        query = query.filter(models.Log.event == event)
    if log_date:
        try:
            day = datetime.strptime(log_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"log_date must be YYYY-MM-DD: {exc}") from exc
        query = query.filter(models.Log.timestamp >= day, models.Log.timestamp < day + timedelta(days=1))

    rows = query.order_by(models.Log.timestamp.desc()).limit(limit).all()
    return [LogEntry(timestamp=r.timestamp, level=r.level, event=r.event, details=r.details) for r in rows]


@router.post("/logs", response_model=LogAck)
def post_log(body: LogIn, db: Session = Depends(get_db)) -> LogAck:
    """Ingests a shipped log event (see trading/common/log_shipper.py) --
    only the curated trading-significant events + WARNING/ERROR, not
    every line the local structured logger emits."""
    server = _resolve_server(db, body.server_id)
    algo = _get_or_create_algo(db, body.algo_id, server)

    db.add(models.Log(
        algo_id=algo.id, server_id=server.id,
        timestamp=body.timestamp or datetime.now(timezone.utc),
        level=body.level.upper(), event=body.event, details=body.details,
    ))
    db.commit()
    return LogAck(success=True)


@router.get("/pnl", response_model=list[DailyPnlEntry])
def get_pnl(algo_id: str, server_id: str, db: Session = Depends(get_db)) -> list[DailyPnlEntry]:
    server = _resolve_server(db, server_id)
    algo = _get_or_create_algo(db, algo_id, server)
    db.commit()

    rows = (
        db.query(models.DailyPnl)
        .filter(models.DailyPnl.algo_id == algo.id)
        .order_by(models.DailyPnl.date.desc())
        .all()
    )
    return [DailyPnlEntry(date=r.date, pnl=r.pnl, trade_count=r.trade_count) for r in rows]


@router.get("/positions", response_model=list[PositionEntry])
def get_positions(algo_id: str, server_id: str, db: Session = Depends(get_db)) -> list[PositionEntry]:
    server = _resolve_server(db, server_id)
    algo = _get_or_create_algo(db, algo_id, server)
    db.commit()

    rows = db.query(models.Position).filter(models.Position.algo_id == algo.id).all()
    return [
        PositionEntry(
            symbol=r.symbol, quantity=r.quantity, average_price=r.average_price,
            last_price=r.last_price, pnl=r.pnl, updated_at=r.updated_at,
        )
        for r in rows
    ]


@router.post("/positions", response_model=PositionAck)
def post_position(body: PositionIn, db: Session = Depends(get_db)) -> PositionAck:
    """Upserts current holdings (unlike trades, which are insert-only
    history) -- a position row represents what's held RIGHT NOW. A
    quantity of 0 means the position closed, so the row is deleted rather
    than kept at zero; "no row" is the correct representation of "no
    position," not a zero-quantity row sitting around forever."""
    server = _resolve_server(db, body.server_id)
    algo = _get_or_create_algo(db, body.algo_id, server)

    existing = (
        db.query(models.Position)
        .filter(
            models.Position.algo_id == algo.id,
            models.Position.server_id == server.id,
            models.Position.symbol == body.symbol,
        )
        .one_or_none()
    )

    if body.quantity == 0:
        if existing is not None:
            db.delete(existing)
            db.commit()
        return PositionAck(success=True, closed=True)

    if existing is not None:
        existing.quantity = body.quantity
        existing.average_price = body.average_price
        existing.last_price = body.last_price
        existing.pnl = body.pnl
    else:
        db.add(models.Position(
            algo_id=algo.id, server_id=server.id, symbol=body.symbol,
            quantity=body.quantity, average_price=body.average_price,
            last_price=body.last_price, pnl=body.pnl,
        ))
    db.commit()
    return PositionAck(success=True, closed=False)


@router.get("/trades", response_model=list[TradeEntry])
def get_trades(algo_id: str, server_id: str, limit: int = 100, db: Session = Depends(get_db)) -> list[TradeEntry]:
    server = _resolve_server(db, server_id)
    algo = _get_or_create_algo(db, algo_id, server)
    db.commit()

    rows = (
        db.query(models.Trade)
        .filter(models.Trade.algo_id == algo.id)
        .order_by(models.Trade.executed_at.desc())
        .limit(limit)
        .all()
    )
    return [
        TradeEntry(
            symbol=r.symbol, side=r.side, quantity=r.quantity, price=r.price,
            executed_at=r.executed_at, order_id=r.order_id,
        )
        for r in rows
    ]


@router.post("/trades", response_model=TradeAck)
def post_trade(body: TradeIn, db: Session = Depends(get_db)) -> TradeAck:
    """Insert-only -- every fill is its own permanent record, never
    updated or deleted (unlike positions, which reflect current state)."""
    server = _resolve_server(db, body.server_id)
    algo = _get_or_create_algo(db, body.algo_id, server)

    db.add(models.Trade(
        algo_id=algo.id, server_id=server.id, symbol=body.symbol, side=body.side.upper(),
        quantity=body.quantity, price=body.price,
        executed_at=body.executed_at or datetime.now(timezone.utc),
        order_id=body.order_id,
    ))
    db.commit()
    return TradeAck(success=True)


@router.post("/pnl", response_model=DailyPnlEntry)
def post_pnl(body: DailyPnlIn, db: Session = Depends(get_db)) -> DailyPnlEntry:
    """Upserts today's (or the given date's) rollup -- one row per
    algo/server/day, overwritten as the strategy's own running total
    changes through the day, not accumulated server-side."""
    server = _resolve_server(db, body.server_id)
    algo = _get_or_create_algo(db, body.algo_id, server)
    target_date = body.pnl_date or datetime.now(timezone.utc).date()

    existing = (
        db.query(models.DailyPnl)
        .filter(
            models.DailyPnl.algo_id == algo.id,
            models.DailyPnl.server_id == server.id,
            models.DailyPnl.date == target_date,
        )
        .one_or_none()
    )
    if existing is not None:
        existing.pnl = body.pnl
        existing.trade_count = body.trade_count
    else:
        db.add(models.DailyPnl(
            algo_id=algo.id, server_id=server.id, date=target_date,
            pnl=body.pnl, trade_count=body.trade_count,
        ))
    db.commit()
    return DailyPnlEntry(date=target_date, pnl=body.pnl, trade_count=body.trade_count)
