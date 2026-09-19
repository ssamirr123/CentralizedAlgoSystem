"""Audit-log writer. One helper, used from routes and deps.

Never raises into the request path: an audit write failing must not take
down the action it is recording (it is logged instead).
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from trading.database import models

logger = logging.getLogger("trading.api.audit")

# Actions we record. Not an enum (routes pass literals) but centralised
# here for grep-ability.
AUTH_LOGIN = "AUTH_LOGIN"
AUTH_LOGIN_FAILED = "AUTH_LOGIN_FAILED"
AUTH_LOGOUT = "AUTH_LOGOUT"
AUTH_REFRESH = "AUTH_REFRESH"
AUTH_REFRESH_DENIED = "AUTH_REFRESH_DENIED"
AUTH_PASSWORD_CHANGED = "AUTH_PASSWORD_CHANGED"
PERMISSION_DENIED = "PERMISSION_DENIED"
ALGO_START = "ALGO_START"
ALGO_STOP = "ALGO_STOP"
ALGO_RESTART = "ALGO_RESTART"
ALGO_UPDATE = "ALGO_UPDATE"
SERVER_REGISTERED = "SERVER_REGISTERED"
SERVER_UPDATED = "SERVER_UPDATED"
SERVER_DELETED = "SERVER_DELETED"
SERVER_START = "SERVER_START"
SERVER_STOP = "SERVER_STOP"
SERVER_RESTART = "SERVER_RESTART"
ALGO_REGISTERED = "ALGO_REGISTERED"
ALGO_PATCHED = "ALGO_PATCHED"
ALGO_DELETED = "ALGO_DELETED"
USER_CREATED = "USER_CREATED"
USER_UPDATED = "USER_UPDATED"
USER_PASSWORD_RESET = "USER_PASSWORD_RESET"
USER_DEACTIVATED = "USER_DEACTIVATED"
MARKET_SESSION_UPDATED = "MARKET_SESSION_UPDATED"
STRATEGY_STARTED = "STRATEGY_STARTED"
STRATEGY_STOPPED = "STRATEGY_STOPPED"
ASSIGNMENT_SET = "ASSIGNMENT_SET"
KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
KILL_SWITCH_DISENGAGED = "KILL_SWITCH_DISENGAGED"
# Phase 16.4 -- strategy control-plane commands (START/STOP), distinct from
# STRATEGY_STARTED/STRATEGY_STOPPED above (which record only the raw Phase
# 11 lifecycle mutation itself, when one actually occurred). These record
# the OUTCOME of a control-plane command, including when no mutation
# happened at all (REJECTED/NOOP).
STRATEGY_COMMAND_ACCEPTED = "STRATEGY_COMMAND_ACCEPTED"
STRATEGY_COMMAND_REJECTED = "STRATEGY_COMMAND_REJECTED"
STRATEGY_COMMAND_NOOP = "STRATEGY_COMMAND_NOOP"
STRATEGY_COMMAND_FAILED = "STRATEGY_COMMAND_FAILED"
# Phase 16.5 -- one explicit PAPER/SHADOW strategy runtime evaluation
# cycle (trading/common/strategy_runtime.py). Records that a cycle ran and
# its outcome; never records a real broker mutation, since none can occur.
STRATEGY_RUNTIME_EVALUATED = "STRATEGY_RUNTIME_EVALUATED"


def record(
    db: Session,
    *,
    actor: str,
    action: str,
    actor_label: str | None = None,
    target: str | None = None,
    outcome: str = "success",
    ip: str | None = None,
    user_agent: str | None = None,
    detail: dict | None = None,
) -> None:
    try:
        db.add(
            models.AuditLog(
                actor=actor,
                actor_label=actor_label,
                action=action,
                target=target,
                outcome=outcome,
                ip=ip,
                user_agent=(user_agent or "")[:255] or None,
                detail=detail,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 -- auditing must never break the request
        logger.exception("audit write failed: action=%s actor=%s", action, actor)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
