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

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from trading.api.deps import get_db, require_api_key
from trading.api.lambda_client import LambdaInvokeError, invoke_orchestrator
from trading.api.schemas import (
    AlgoActionRequest,
    AlgoStatusResponse,
    CommandResponse,
    DailyPnlEntry,
    LogEntry,
    PositionEntry,
    ServerStatusResponse,
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


@router.get("/logs", response_model=list[LogEntry])
def get_logs(algo_id: str, server_id: str, limit: int = 100, db: Session = Depends(get_db)) -> list[LogEntry]:
    server = _resolve_server(db, server_id)
    algo = _get_or_create_algo(db, algo_id, server)
    db.commit()

    rows = (
        db.query(models.Log)
        .filter(models.Log.algo_id == algo.id)
        .order_by(models.Log.timestamp.desc())
        .limit(limit)
        .all()
    )
    return [LogEntry(timestamp=r.timestamp, level=r.level, event=r.event, details=r.details) for r in rows]


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
