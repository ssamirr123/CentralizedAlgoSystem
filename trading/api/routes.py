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

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alerts.telegram import alert_service
from trading.api.deps import enforce_rate_limit, get_db, require_api_key
from trading.api.lambda_client import LambdaInvokeError, invoke_orchestrator, invoke_orchestrator_async
from trading.api.schemas import (
    AlgoActionRequest,
    AlgoIn,
    AlgoListEntry,
    AlgoRegisterResponse,
    AlgoStatusResponse,
    AlgoUpdate,
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
    ServerIn,
    ServerListEntry,
    ServerStatusResponse,
    ServerUpdate,
    TradeAck,
    TradeEntry,
    TradeIn,
)
from trading.database import models

router = APIRouter(dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)])
logger = logging.getLogger("trading.api")

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
    """Auto-create fallback disabled: an algo must now be explicitly
    registered via POST /api/algos before any start/stop/heartbeat/log
    call will touch it. Previously this silently inserted a brand-new
    algos row on first contact, which meant a typo'd algo_id or a stray
    heartbeat from a decommissioned strategy would quietly reappear on
    the dashboard instead of failing loudly."""
    algo = (
        db.query(models.Algo)
        .filter(models.Algo.name == algo_name, models.Algo.server_id == server.id)
        .one_or_none()
    )
    if algo is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Algo not registered: {algo_name} on {server.name}. Register it via POST /api/algos first.",
        )
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
        result = invoke_orchestrator(
            lambda_action, algo_id=body.algo_id, instance_id=server.ec2_instance_id,
            repo_path=server.repo_path, os_name=server.os, server_name=server.name,
        )
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
        # command_row.server_id is the FK (int PK), not the server's
        # name -- resolve it to get the real instance_id this specific
        # command was actually sent to (per-server routing means the
        # Lambda can no longer assume a single fixed target).
        command_server = db.query(models.Server).filter(models.Server.id == command_row.server_id).one_or_none()
        if command_server is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Server for command {command_id} no longer exists")
        try:
            result = invoke_orchestrator(
                "get_command_status", job_id=command_row.job_id, instance_id=command_server.ec2_instance_id,
            )
        except LambdaInvokeError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not reach Lambda: {exc}") from exc

        if result.get("status") != "IN_PROGRESS":
            command_row.status = result.get("status", command_row.status)
            command_row.result = result
            if not result.get("success"):
                command_row.error = result.get("error") or result.get("message")

            # Sync the real, verified outcome back onto the algo itself --
            # without this, algos.status stays stuck at whatever the last
            # heartbeat said (typically RUNNING) forever after a stop,
            # since a stopped process sends no further heartbeats to ever
            # correct it. This is trading_agent.py's own reported status
            # (process-liveness checked), the most authoritative source
            # available, not a guess derived from the command type.
            if command_row.algo_id is not None and result.get("status"):
                algo_row = db.query(models.Algo).filter(models.Algo.id == command_row.algo_id).one_or_none()
                if algo_row is not None:
                    algo_row.status = result["status"]

            db.commit()

    return CommandResponse(
        success=command_row.status not in ("FAILED", "ERROR", "UNKNOWN"),
        command_id=command_row.id,
        job_id=command_row.job_id,
        status=command_row.status,
        message=command_row.error,
    )


@router.get("/algo/status", response_model=AlgoStatusResponse)
def algo_status(algo_id: str, server_id: str, db: Session = Depends(get_db)) -> AlgoStatusResponse:
    server = _resolve_server(db, server_id)
    try:
        result = invoke_orchestrator(
            "get_algo_status", algo_id=algo_id, instance_id=server.ec2_instance_id,
            repo_path=server.repo_path, os_name=server.os,
        )
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
def server_status(server_id: str, live: bool = False, db: Session = Depends(get_db)) -> ServerStatusResponse:
    server = _resolve_server(db, server_id)

    ssm_status = None
    live_check_healthy = None
    if live:
        try:
            result = invoke_orchestrator("check_ec2_health", instance_id=server.ec2_instance_id)
            if result.get("success"):
                server.status = result.get("ec2_status", server.status)
                ssm_status = result.get("ssm_status")
                live_check_healthy = result.get("healthy")
                db.commit()
        except LambdaInvokeError as exc:
            # A failed live check degrades to the cached DB value rather
            # than failing the whole request -- "can't verify right now"
            # is not the same as "definitely unhealthy."
            logger.warning("check_ec2_health failed for %s: %s", server_id, exc)

    return ServerStatusResponse(
        name=server.name,
        ec2_instance_id=server.ec2_instance_id,
        region=server.region,
        status=server.status,
        last_heartbeat=server.last_heartbeat,
        ssm_status=ssm_status,
        live_check_healthy=live_check_healthy,
    )


def _server_entry(server: models.Server) -> ServerListEntry:
    return ServerListEntry(
        server_id=server.name, ec2_instance_id=server.ec2_instance_id,
        region=server.region, status=server.status, os=server.os, repo_path=server.repo_path,
        provisioning_status=server.provisioning_status, provisioning_message=server.provisioning_message,
        last_heartbeat=server.last_heartbeat,
    )


@router.post("/servers", response_model=ServerListEntry, status_code=status.HTTP_201_CREATED)
def register_server(body: ServerIn, db: Session = Depends(get_db)) -> ServerListEntry:
    """Registers a new EC2 trading server. There's no auto-registration
    path (heartbeats/logs/etc. all require the server to already exist,
    via _resolve_server) -- this is the one place a servers row gets
    created, meant to be called once per EC2 instance during setup.

    auto_provision=True (the default) kicks off the async provision_server
    Lambda action right after commit -- attach the IAM instance profile,
    reboot if SSM isn't already registered, wait for it to come online,
    clone the repo, install deps. That's a multi-minute process (a reboot
    alone can take a minute-plus), which is exactly why it's fired async
    (InvocationType=Event) instead of the synchronous pattern every other
    Lambda action here uses -- a single Vercel request can't wait that
    long. The Lambda reports its own progress back via PATCH on this same
    server once it's done, not this request."""
    server = models.Server(
        name=body.server_id, ec2_instance_id=body.ec2_instance_id,
        region=body.region, status=body.status, os=body.os, repo_path=body.repo_path,
        provisioning_status="PROVISIONING" if body.auto_provision else "READY",
    )
    db.add(server)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Server already registered: {body.server_id}"
        ) from None
    db.refresh(server)

    if body.auto_provision:
        try:
            invoke_orchestrator_async(
                "provision_server", instance_id=server.ec2_instance_id,
                os_name=server.os, repo_path=server.repo_path, server_name=server.name,
            )
        except LambdaInvokeError as exc:
            server.provisioning_status = "FAILED"
            server.provisioning_message = f"Could not start provisioning: {exc}"
            db.commit()

    return _server_entry(server)


@router.get("/servers", response_model=list[ServerListEntry])
def list_servers(db: Session = Depends(get_db)) -> list[ServerListEntry]:
    rows = db.query(models.Server).order_by(models.Server.name).all()
    return [_server_entry(r) for r in rows]


@router.patch("/servers/{server_id}", response_model=ServerListEntry)
def update_server(server_id: str, body: ServerUpdate, db: Session = Depends(get_db)) -> ServerListEntry:
    server = _resolve_server(db, server_id)

    if body.server_id is not None:
        server.name = body.server_id
    if body.ec2_instance_id is not None:
        server.ec2_instance_id = body.ec2_instance_id
    if body.region is not None:
        server.region = body.region
    if body.status is not None:
        server.status = body.status
    if body.os is not None:
        server.os = body.os
    if body.repo_path is not None:
        server.repo_path = body.repo_path
    if body.provisioning_status is not None:
        server.provisioning_status = body.provisioning_status
    if body.provisioning_message is not None:
        server.provisioning_message = body.provisioning_message

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Server already registered: {body.server_id}"
        ) from None
    db.refresh(server)
    return _server_entry(server)


@router.delete("/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_server(server_id: str, db: Session = Depends(get_db)) -> Response:
    """Refuses to delete a server that still has algos registered against
    it -- the caller has to remove/reassign those first, rather than this
    endpoint silently cascading through heartbeats/logs/positions/trades/
    commands for every algo that ever ran there."""
    server = _resolve_server(db, server_id)

    algo_count = db.query(models.Algo).filter(models.Algo.server_id == server.id).count()
    if algo_count:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot delete server '{server_id}': {algo_count} algo(s) still registered against it.",
        )

    db.delete(server)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Cannot delete server '{server_id}': it still has related records."
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/algos", response_model=AlgoRegisterResponse, status_code=status.HTTP_201_CREATED)
def register_algo(body: AlgoIn, db: Session = Depends(get_db)) -> AlgoRegisterResponse:
    """Registers a new strategy for viewing/management before it's ever
    been started. This is now the ONLY way an algo row gets created --
    start/stop/heartbeat/log calls against an unregistered algo_id are
    rejected with 404 (see _get_or_create_algo) rather than silently
    creating one, so a typo'd algo_id or a stray call from a
    decommissioned strategy can't quietly reappear on the dashboard.

    Also triggers a best-effort code sync (git pull) on the target EC2
    instance right after registering -- a DB row alone puts no file on
    disk, so without this, START would fail with AlgoNotFoundError for
    any strategy whose code hasn't been separately deployed by hand.
    Sync failure doesn't fail registration or roll it back; it's reported
    back to the caller so the dashboard can surface it, but the DB row
    (the source of truth for "this strategy exists") stands regardless.

    Targets body.server_id's own instance_id/repo_path/os -- per-server
    routing, not a single shared target."""
    server = _resolve_server(db, body.server_id)

    existing = (
        db.query(models.Algo)
        .filter(models.Algo.name == body.algo_id, models.Algo.server_id == server.id)
        .one_or_none()
    )
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Algo already registered: {body.algo_id} on {body.server_id}"
        )

    algo = models.Algo(
        name=body.algo_id,
        server_id=server.id,
        script_path=body.script_path or f"trading/algos/{body.algo_id}/main.py",
        status=body.status,
        enabled=body.enabled,
    )
    db.add(algo)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Algo already registered: {body.algo_id} on {body.server_id}"
        ) from None
    db.refresh(algo)

    algo_entry = AlgoListEntry(
        algo_id=algo.name, server_id=server.name, status=algo.status,
        enabled=algo.enabled, script_path=algo.script_path, updated_at=algo.updated_at,
        last_heartbeat=None,
    )

    sync_success = None
    sync_message = None
    try:
        sync_result = invoke_orchestrator(
            "sync_repo", instance_id=server.ec2_instance_id, repo_path=server.repo_path, os_name=server.os,
            algo_id=body.algo_id,
        )
        sync_success = bool(sync_result.get("success"))
        sync_message = sync_result.get("output") or sync_result.get("error") or sync_result.get("message")
    except LambdaInvokeError as exc:
        sync_success = False
        sync_message = str(exc)

    return AlgoRegisterResponse(
        algo=algo_entry, sync_attempted=True, sync_success=sync_success, sync_message=sync_message,
    )


@router.patch("/algos/{algo_id}", response_model=AlgoListEntry)
def patch_algo(algo_id: str, server_id: str, body: AlgoUpdate, db: Session = Depends(get_db)) -> AlgoListEntry:
    server = _resolve_server(db, server_id)
    algo = (
        db.query(models.Algo)
        .filter(models.Algo.name == algo_id, models.Algo.server_id == server.id)
        .one_or_none()
    )
    if algo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown algo: {algo_id} on {server_id}")

    if body.script_path is not None:
        algo.script_path = body.script_path
    if body.status is not None:
        algo.status = body.status
    if body.enabled is not None:
        algo.enabled = body.enabled
    db.commit()
    db.refresh(algo)

    last_heartbeat = (
        db.query(func.max(models.Heartbeat.timestamp))
        .filter(models.Heartbeat.algo_id == algo.id)
        .scalar()
    )
    return AlgoListEntry(
        algo_id=algo.name, server_id=server.name, status=algo.status,
        enabled=algo.enabled, script_path=algo.script_path, updated_at=algo.updated_at,
        last_heartbeat=last_heartbeat,
    )


@router.delete("/algos/{algo_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_algo(algo_id: str, server_id: str, force: bool = False, db: Session = Depends(get_db)) -> Response:
    """Refuses to delete an algo that still has heartbeat/log/position/
    trade/P&L/command/run history -- same reasoning as delete_server:
    surface exactly what's blocking it rather than silently cascading
    through years of trading history. force=true is the explicit opt-in
    to purge that history and the algo together, for when the caller
    really does want it gone (e.g. a test registration)."""
    server = _resolve_server(db, server_id)
    algo = (
        db.query(models.Algo)
        .filter(models.Algo.name == algo_id, models.Algo.server_id == server.id)
        .one_or_none()
    )
    if algo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown algo: {algo_id} on {server_id}")

    related_models = {
        "heartbeat(s)": models.Heartbeat,
        "log(s)": models.Log,
        "position(s)": models.Position,
        "trade(s)": models.Trade,
        "daily P&L row(s)": models.DailyPnl,
        "command(s)": models.Command,
        "run(s)": models.AlgoRun,
    }
    related_counts = {
        label: db.query(model).filter(model.algo_id == algo.id).count() for label, model in related_models.items()
    }
    blocking = {label: count for label, count in related_counts.items() if count}
    if blocking and not force:
        detail = ", ".join(f"{count} {label}" for label, count in blocking.items())
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot delete algo '{algo_id}' on '{server_id}': still has {detail}. "
            "Retry with ?force=true to also purge this history.",
        )

    if blocking:
        for label in blocking:
            db.query(related_models[label]).filter(related_models[label].algo_id == algo.id).delete()

    db.delete(algo)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Cannot delete algo '{algo_id}': it still has related records."
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    state, a separate concern from an individual algo's health.

    Milestone 12: fires the existing, already-tested Telegram alert_service
    on status transitions -- the old backend/main.py has this for the old
    schema; the new one (Milestone 8+) had none until now. Only fires on
    a CHANGE (or a brand-new algo's first heartbeat), not every heartbeat,
    same dedup principle as the old path. Note what this can't cover:
    Vercel serverless has no background process to notice SILENCE (an algo
    that stops heartbeating entirely rather than reporting ERROR) --
    that's the dashboard's client-side staleness detection (Milestone 8),
    not a server-side alert. Catching that server-side would need a
    scheduled check (extending Milestone 11), not this endpoint.
    """
    server = _resolve_server(db, body.server_id)

    # Auto-create fallback disabled (see _get_or_create_algo) -- an algo
    # must already be registered via POST /api/algos before it can ever
    # heartbeat, so "brand new, first heartbeat ever" can no longer
    # happen here. previous_status always reflects a real prior row
    # (registration's own status default, or whatever it last reported).
    algo = _get_or_create_algo(db, body.algo_id, server)
    previous_status = algo.status

    ts = body.timestamp or datetime.now(timezone.utc)

    db.add(models.Heartbeat(
        algo_id=algo.id, server_id=server.id, timestamp=ts, status=body.status,
        cpu=body.cpu, memory=body.memory, pnl=body.pnl, position=body.position,
    ))
    algo.status = body.status
    server.last_heartbeat = ts
    db.commit()

    if previous_status != body.status:
        if body.status == "RUNNING":
            alert_service.strategy_recovered(body.algo_id, body.server_id)
        elif body.status == "STOPPED":
            alert_service.strategy_stopped(body.algo_id, body.server_id)
        elif body.status == "ERROR":
            alert_service.strategy_crashed(body.algo_id, body.server_id, reason="Status changed to ERROR")

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


@router.get("/pnl/today", response_model=dict[str, float])
def get_today_pnl_bulk(pnl_date: str | None = None, db: Session = Depends(get_db)) -> dict[str, float]:
    """Bulk equivalent of GET /pnl, filtered to one calendar day -- one
    query instead of one HTTP round trip per algo. The dashboard's header
    P&L total and per-row P&L column both used to loop over every algo
    calling GET /pnl individually; against this project's current
    per-request latency (~4-5s, see the NullPool tradeoff), N algos meant
    N sequential blocking calls before the page could even render.

    pnl_date defaults to today in UTC (matching POST /api/pnl's own
    default) -- pass it explicitly (YYYY-MM-DD) for IST "today", since
    DailyPnl.date is a plain Date with no stored timezone and the caller
    is in the best position to know which calendar day it actually means.

    Keyed by "algo_id|server_id", not algo_id alone -- algo names are
    only unique per server (uq_algo_name_server), not globally.
    """
    if pnl_date:
        try:
            target_date = datetime.strptime(pnl_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"pnl_date must be YYYY-MM-DD: {exc}") from exc
    else:
        target_date = datetime.now(timezone.utc).date()

    rows = (
        db.query(models.DailyPnl, models.Algo.name, models.Server.name)
        .join(models.Algo, models.DailyPnl.algo_id == models.Algo.id)
        .join(models.Server, models.DailyPnl.server_id == models.Server.id)
        .filter(models.DailyPnl.date == target_date)
        .all()
    )
    return {f"{algo_name}|{server_name}": pnl_row.pnl for pnl_row, algo_name, server_name in rows}


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
