"""
Control-center API routes, mounted under /api by create_app().

Algo control actions (start/stop/restart/update) are async: create a
Command audit row, invoke the orchestration Lambda, store the returned
job_id, return immediately. The caller polls GET /api/command/{id} for
the real outcome -- this endpoint never claims RUNNING just because the
Lambda accepted the request.

GET endpoints (server/status, logs, pnl, positions) read straight from
PostgreSQL, no Lambda call.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alerts.telegram import alert_service
from trading.core.config import load_settings
from trading.api.deps import (
    Principal,
    client_ip,
    enforce_rate_limit,
    get_db,
    get_principal,
    require_ingest,
    require_permission,
)
from trading.api.security import audit
from trading.api.security.permissions import Permission
from trading.api.realtime import publish as rt
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
    ServerPowerResponse,
    ServerStatusResponse,
    ServerUpdate,
    TradeAck,
    TradeEntry,
    TradeIn,
)
from trading.database import models

# Every route authenticates (Bearer user OR X-API-Key service) and is
# rate-limited per identity. Individual routes add
# Depends(require_permission(...)) for the capability they need.
router = APIRouter(dependencies=[Depends(get_principal), Depends(enforce_rate_limit)])
logger = logging.getLogger("trading.api")

_ACTION_TO_AGENT_COMMAND = {
    "start": "START_ALGO",
    "stop": "STOP_ALGO",
    "restart": "RESTART_ALGO",
    "update": "UPDATE",
}

_ACTION_PERMISSION = {
    "start": Permission.START,
    "stop": Permission.STOP,
    "restart": Permission.RESTART,
    "update": Permission.TRADING_CONTROL,
}

_ACTION_AUDIT = {
    "start": audit.ALGO_START,
    "stop": audit.ALGO_STOP,
    "restart": audit.ALGO_RESTART,
    "update": audit.ALGO_UPDATE,
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


def _run_algo_action(
    action: str, body: AlgoActionRequest, db: Session, request: Request, principal: Principal
) -> CommandResponse:
    server = _resolve_server(db, body.server_id)
    algo = _get_or_create_algo(db, body.algo_id, server)

    # The authenticated identity is authoritative for "who did this" --
    # a client-supplied requested_by is only a fallback label.
    requested_by = principal.label or body.requested_by

    command_row = models.Command(
        algo_id=algo.id,
        server_id=server.id,
        command=_ACTION_TO_AGENT_COMMAND[action],
        requested_by=requested_by,
        status="PENDING",
    )
    db.add(command_row)
    db.commit()
    db.refresh(command_row)

    audit.record(
        db, actor=principal.actor, actor_label=principal.label, action=_ACTION_AUDIT[action],
        target=f"algo:{body.algo_id}@{body.server_id}", ip=client_ip(request),
        user_agent=request.headers.get("user-agent"), detail={"command_id": command_row.id},
    )

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
        rt.command(command_id=command_row.id, algo_id=body.algo_id, server_id=body.server_id,
                   action=action, status="FAILED", requested_by=requested_by, message=str(exc))
        return CommandResponse(success=False, command_id=command_row.id, status="FAILED", message=str(exc))

    command_row.job_id = result.get("job_id")
    command_row.status = result.get("status", "FAILED")
    command_row.result = result
    if not result.get("success"):
        command_row.error = result.get("error")
    db.commit()

    rt.command(
        command_id=command_row.id, algo_id=body.algo_id, server_id=body.server_id, action=action,
        status=result.get("status", "UNKNOWN"), job_id=result.get("job_id"),
        requested_by=requested_by, message=result.get("error") or result.get("message"),
    )
    return CommandResponse(
        success=bool(result.get("success")),
        command_id=command_row.id,
        job_id=result.get("job_id"),
        status=result.get("status", "UNKNOWN"),
        message=result.get("error") or result.get("message"),
    )


@router.post("/algo/start", response_model=CommandResponse)
def start_algo(
    body: AlgoActionRequest, request: Request, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.START)),
) -> CommandResponse:
    return _run_algo_action("start", body, db, request, principal)


@router.post("/algo/stop", response_model=CommandResponse)
def stop_algo(
    body: AlgoActionRequest, request: Request, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.STOP)),
) -> CommandResponse:
    return _run_algo_action("stop", body, db, request, principal)


@router.post("/algo/restart", response_model=CommandResponse)
def restart_algo(
    body: AlgoActionRequest, request: Request, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.RESTART)),
) -> CommandResponse:
    return _run_algo_action("restart", body, db, request, principal)


@router.post("/algo/update", response_model=CommandResponse)
def update_algo(
    body: AlgoActionRequest, request: Request, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.TRADING_CONTROL)),
) -> CommandResponse:
    return _run_algo_action("update", body, db, request, principal)


@router.get("/command/{command_id}", response_model=CommandResponse)
def get_command(
    command_id: int, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.VIEW)),
) -> CommandResponse:
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
            synced_algo_status = None
            algo_name = None
            if command_row.algo_id is not None and result.get("status"):
                algo_row = db.query(models.Algo).filter(models.Algo.id == command_row.algo_id).one_or_none()
                if algo_row is not None:
                    algo_row.status = result["status"]
                    synced_algo_status = result["status"]
                    algo_name = algo_row.name

            db.commit()

            rt.command(
                command_id=command_row.id, algo_id=algo_name, server_id=command_server.name,
                action=command_row.command, status=command_row.status, job_id=command_row.job_id,
                requested_by=command_row.requested_by, message=command_row.error,
            )
            if synced_algo_status and algo_name:
                rt.strategy_status(
                    algo_name, command_server.name, status=synced_algo_status, source="command",
                )

    return CommandResponse(
        success=command_row.status not in ("FAILED", "ERROR", "UNKNOWN"),
        command_id=command_row.id,
        job_id=command_row.job_id,
        status=command_row.status,
        message=command_row.error,
    )


@router.get("/algo/status", response_model=AlgoStatusResponse)
def algo_status(
    algo_id: str, server_id: str, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.VIEW)),
) -> AlgoStatusResponse:
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
def server_status(
    server_id: str, live: bool = False, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.VIEW)),
) -> ServerStatusResponse:
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
                rt.server_health(
                    server.name, status=server.status, ssm_status=ssm_status,
                    healthy=live_check_healthy,
                    last_heartbeat=server.last_heartbeat.isoformat() if server.last_heartbeat else None,
                    source="live_check",
                )
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
def register_server(
    body: ServerIn, request: Request, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.TRADING_CONTROL)),
) -> ServerListEntry:
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

    audit.record(
        db, actor=principal.actor, actor_label=principal.label, action=audit.SERVER_REGISTERED,
        target=f"server:{server.name}", ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"ec2_instance_id": server.ec2_instance_id, "auto_provision": body.auto_provision},
    )
    return _server_entry(server)


@router.get("/servers", response_model=list[ServerListEntry])
def list_servers(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.VIEW)),
) -> list[ServerListEntry]:
    rows = db.query(models.Server).order_by(models.Server.name).all()
    return [_server_entry(r) for r in rows]


@router.patch("/servers/{server_id}", response_model=ServerListEntry)
def update_server(
    server_id: str, body: ServerUpdate, request: Request, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.TRADING_CONTROL)),
) -> ServerListEntry:
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
    audit.record(
        db, actor=principal.actor, actor_label=principal.label, action=audit.SERVER_UPDATED,
        target=f"server:{server.name}", ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail=body.model_dump(exclude_none=True),
    )
    return _server_entry(server)


@router.delete("/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_server(
    server_id: str, request: Request, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.TRADING_CONTROL)),
) -> Response:
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
    audit.record(
        db, actor=principal.actor, actor_label=principal.label, action=audit.SERVER_DELETED,
        target=f"server:{server_id}", ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_SERVER_POWER = {
    "start": ("start_ec2", audit.SERVER_START),
    "stop": ("stop_ec2", audit.SERVER_STOP),
    "restart": ("restart_ec2", audit.SERVER_RESTART),
}


def _run_server_power(
    action: str, server_id: str, force: bool, request: Request, db: Session, principal: Principal
) -> ServerPowerResponse:
    """EC2 power control, routed React -> FastAPI -> Lambda -> EC2. Never
    talks to AWS from anything but the orchestrator Lambda. The
    orchestrator's own safe-stop guard (a trading process still alive
    blocks stop/restart unless force=true) is authoritative -- this route
    surfaces that block as a 409, it does not re-implement or bypass it."""
    lambda_action, audit_action = _SERVER_POWER[action]
    server = _resolve_server(db, server_id)

    try:
        result = invoke_orchestrator(
            lambda_action, instance_id=server.ec2_instance_id, os_name=server.os,
            server_name=server.name, force=force,
        )
    except LambdaInvokeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not reach Lambda: {exc}") from exc

    audit.record(
        db, actor=principal.actor, actor_label=principal.label, action=audit_action,
        target=f"server:{server.name}", ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"force": force, "result_status": result.get("status"), "success": result.get("success")},
    )

    if not result.get("success"):
        # A safe-stop block is a client-actionable condition (stop the
        # algos first, or pass force=true), not a server fault -> 409.
        if result.get("safe_stop") in ("blocked", "unverified"):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                result.get("error") or "Stop blocked: trading processes still alive on this server.",
            )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            result.get("error") or f"{lambda_action} failed",
        )

    new_status = result.get("status", server.status)
    server.status = new_status
    db.commit()
    rt.server_health(
        server.name, status=new_status, ssm_status=None, healthy=None,
        last_heartbeat=server.last_heartbeat.isoformat() if server.last_heartbeat else None,
        source=f"power_{action}",
    )
    return ServerPowerResponse(
        success=True, server_id=server.name, ec2_instance_id=server.ec2_instance_id,
        status=new_status, message=result.get("message"),
    )


@router.post("/servers/{server_id}/start", response_model=ServerPowerResponse)
def start_server(
    server_id: str, request: Request, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.TRADING_CONTROL)),
) -> ServerPowerResponse:
    return _run_server_power("start", server_id, False, request, db, principal)


@router.post("/servers/{server_id}/stop", response_model=ServerPowerResponse)
def stop_server(
    server_id: str, request: Request, force: bool = False, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.TRADING_CONTROL)),
) -> ServerPowerResponse:
    return _run_server_power("stop", server_id, force, request, db, principal)


@router.post("/servers/{server_id}/restart", response_model=ServerPowerResponse)
def restart_server(
    server_id: str, request: Request, force: bool = False, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.TRADING_CONTROL)),
) -> ServerPowerResponse:
    return _run_server_power("restart", server_id, force, request, db, principal)


@router.post("/algos", response_model=AlgoRegisterResponse, status_code=status.HTTP_201_CREATED)
def register_algo(
    body: AlgoIn, request: Request, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.TRADING_CONTROL)),
) -> AlgoRegisterResponse:
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

    audit.record(
        db, actor=principal.actor, actor_label=principal.label, action=audit.ALGO_REGISTERED,
        target=f"algo:{body.algo_id}@{body.server_id}", ip=client_ip(request),
        user_agent=request.headers.get("user-agent"), detail={"sync_success": sync_success},
    )
    return AlgoRegisterResponse(
        algo=algo_entry, sync_attempted=True, sync_success=sync_success, sync_message=sync_message,
    )


@router.patch("/algos/{algo_id}", response_model=AlgoListEntry)
def patch_algo(
    algo_id: str, server_id: str, body: AlgoUpdate, request: Request, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.TRADING_CONTROL)),
) -> AlgoListEntry:
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

    audit.record(
        db, actor=principal.actor, actor_label=principal.label, action=audit.ALGO_PATCHED,
        target=f"algo:{algo_id}@{server_id}", ip=client_ip(request),
        user_agent=request.headers.get("user-agent"), detail=body.model_dump(exclude_none=True),
    )
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
def delete_algo(
    algo_id: str, server_id: str, request: Request, force: bool = False, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.TRADING_CONTROL)),
) -> Response:
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
    audit.record(
        db, actor=principal.actor, actor_label=principal.label, action=audit.ALGO_DELETED,
        target=f"algo:{algo_id}@{server_id}", ip=client_ip(request),
        user_agent=request.headers.get("user-agent"), detail={"force": force, "purged": blocking},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/algos", response_model=list[AlgoListEntry])
def list_algos(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.VIEW)),
) -> list[AlgoListEntry]:
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
def post_heartbeat(
    body: HeartbeatIn, db: Session = Depends(get_db),
    principal: Principal = Depends(require_ingest),
) -> HeartbeatAck:
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

    rt.heartbeat(
        body.algo_id, body.server_id, status=body.status, cpu=body.cpu, memory=body.memory,
        pnl=body.pnl, position=body.position, timestamp=ts.isoformat(),
    )

    if previous_status != body.status:
        rt.strategy_status(
            body.algo_id, body.server_id, status=body.status,
            previous_status=previous_status, source="heartbeat",
        )
        if body.status == "RUNNING":
            alert_service.strategy_recovered(body.algo_id, body.server_id)
            rt.alert(kind="strategy_recovered", severity="info",
                     message=f"{body.algo_id} recovered on {body.server_id}",
                     algo_id=body.algo_id, server_id=body.server_id)
        elif body.status == "STOPPED":
            alert_service.strategy_stopped(body.algo_id, body.server_id)
            rt.alert(kind="strategy_stopped", severity="warning",
                     message=f"{body.algo_id} stopped on {body.server_id}",
                     algo_id=body.algo_id, server_id=body.server_id)
        elif body.status == "ERROR":
            alert_service.strategy_crashed(body.algo_id, body.server_id, reason="Status changed to ERROR")
            rt.alert(kind="strategy_crashed", severity="critical",
                     message=f"{body.algo_id} entered ERROR on {body.server_id}",
                     algo_id=body.algo_id, server_id=body.server_id)

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
    principal: Principal = Depends(require_permission(Permission.VIEW)),
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
def post_log(
    body: LogIn, db: Session = Depends(get_db),
    principal: Principal = Depends(require_ingest),
) -> LogAck:
    """Ingests a shipped log event (see trading/common/log_shipper.py) --
    only the curated trading-significant events + WARNING/ERROR, not
    every line the local structured logger emits."""
    server = _resolve_server(db, body.server_id)
    algo = _get_or_create_algo(db, body.algo_id, server)

    level = body.level.upper()
    db.add(models.Log(
        algo_id=algo.id, server_id=server.id,
        timestamp=body.timestamp or datetime.now(timezone.utc),
        level=level, event=body.event, details=body.details,
    ))
    db.commit()
    if level in ("ERROR", "WARNING"):
        rt.alert(
            kind="log", severity="critical" if level == "ERROR" else "warning",
            message=f"{body.event} ({body.algo_id})", algo_id=body.algo_id,
            server_id=body.server_id, detail=body.details,
        )
    return LogAck(success=True)


@router.get("/pnl/today", response_model=dict[str, float])
def get_today_pnl_bulk(
    pnl_date: str | None = None, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.VIEW)),
) -> dict[str, float]:
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
def get_pnl(
    algo_id: str, server_id: str, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.VIEW)),
) -> list[DailyPnlEntry]:
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
def get_positions(
    algo_id: str, server_id: str, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.VIEW)),
) -> list[PositionEntry]:
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
def post_position(
    body: PositionIn, db: Session = Depends(get_db),
    principal: Principal = Depends(require_ingest),
) -> PositionAck:
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
        rt.position(body.algo_id, body.server_id, symbol=body.symbol, quantity=0, closed=True)
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
    rt.position(
        body.algo_id, body.server_id, symbol=body.symbol, quantity=body.quantity,
        average_price=body.average_price, last_price=body.last_price, pnl=body.pnl, closed=False,
    )
    return PositionAck(success=True, closed=False)


@router.get("/trades", response_model=list[TradeEntry])
def get_trades(
    algo_id: str, server_id: str, limit: int = 100, db: Session = Depends(get_db),
    principal: Principal = Depends(require_permission(Permission.VIEW)),
) -> list[TradeEntry]:
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
def post_trade(
    body: TradeIn, db: Session = Depends(get_db),
    principal: Principal = Depends(require_ingest),
) -> TradeAck:
    """Insert-only -- every fill is its own permanent record, never
    updated or deleted (unlike positions, which reflect current state)."""
    server = _resolve_server(db, body.server_id)
    algo = _get_or_create_algo(db, body.algo_id, server)

    executed_at = body.executed_at or datetime.now(timezone.utc)
    db.add(models.Trade(
        algo_id=algo.id, server_id=server.id, symbol=body.symbol, side=body.side.upper(),
        quantity=body.quantity, price=body.price,
        executed_at=executed_at,
        order_id=body.order_id,
    ))
    db.commit()
    rt.trade(
        body.algo_id, body.server_id, symbol=body.symbol, side=body.side.upper(),
        quantity=body.quantity, price=body.price, executed_at=executed_at.isoformat(),
        order_id=body.order_id,
    )
    return TradeAck(success=True)


@router.post("/pnl", response_model=DailyPnlEntry)
def post_pnl(
    body: DailyPnlIn, db: Session = Depends(get_db),
    principal: Principal = Depends(require_ingest),
) -> DailyPnlEntry:
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
    rt.pnl(
        body.algo_id, body.server_id, date=target_date.isoformat(),
        pnl=body.pnl, trade_count=body.trade_count,
    )

    # Day-loss alert -- fires whenever the reported daily P&L breaches the
    # configured limit. alert_service dedups so it won't spam on every
    # rollup update. (This preserved the behaviour of the removed legacy
    # /update_strategy path.)
    day_loss_limit = load_settings().day_loss_limit
    if body.pnl < 0 and abs(body.pnl) > day_loss_limit:
        alert_service.day_loss_exceeded(
            body.algo_id, body.server_id, loss=body.pnl, limit=day_loss_limit
        )
        rt.alert(
            kind="day_loss_exceeded", severity="critical",
            message=f"{body.algo_id} day loss {body.pnl:.0f} exceeds limit {day_loss_limit:.0f}",
            algo_id=body.algo_id, server_id=body.server_id,
        )

    return DailyPnlEntry(date=target_date, pnl=body.pnl, trade_count=body.trade_count)
