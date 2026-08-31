"""
User administration + audit-log access. Everything here requires the
ADMIN permission.

    GET    /api/admin/users
    POST   /api/admin/users                      {username, password, role, email?, extra_permissions?}
    PATCH  /api/admin/users/{id}                  {role?, is_active?, email?, extra_permissions?}
    POST   /api/admin/users/{id}/reset-password   {new_password}
    DELETE /api/admin/users/{id}                  (soft: deactivate + revoke sessions)
    GET    /api/admin/audit                       ?actor=&action=&outcome=&since=&limit=
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trading.api.deps import Principal, client_ip, get_db, require_permission
from trading.api.security import audit
from trading.api.security.passwords import WeakPasswordError, hash_password, validate_password_strength
from trading.api.security.permissions import (
    DEFAULT_ROLE,
    VALID_ROLES,
    Permission,
    normalize_extra_permissions,
    permissions_for,
)
from trading.database import models

router = APIRouter(prefix="/admin", tags=["admin"])

_ADMIN = require_permission(Permission.ADMIN)


class UserAdminOut(BaseModel):
    id: int
    username: str
    email: str | None
    role: str
    extra_permissions: list[str]
    effective_permissions: list[str]
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None
    created_at: datetime


class CreateUserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=1, max_length=256)
    role: str = DEFAULT_ROLE
    email: str | None = Field(default=None, max_length=255)
    extra_permissions: list[str] | None = None


class UpdateUserIn(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    email: str | None = Field(default=None, max_length=255)
    extra_permissions: list[str] | None = None


class ResetPasswordIn(BaseModel):
    new_password: str = Field(min_length=1, max_length=256)


def _out(u: models.User) -> UserAdminOut:
    return UserAdminOut(
        id=u.id,
        username=u.username,
        email=u.email,
        role=u.role,
        extra_permissions=list(u.extra_permissions or []),
        effective_permissions=sorted(p.value for p in permissions_for(u.role, u.extra_permissions)),
        is_active=u.is_active,
        must_change_password=u.must_change_password,
        last_login_at=u.last_login_at,
        created_at=u.created_at,
    )


def _validate_role(role: str) -> None:
    if role not in VALID_ROLES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"role must be one of {list(VALID_ROLES)}")


def _active_admin_count(db: Session, exclude_id: int | None = None) -> int:
    q = db.query(models.User).filter(models.User.role == "admin", models.User.is_active.is_(True))
    if exclude_id is not None:
        q = q.filter(models.User.id != exclude_id)
    return q.count()


def _revoke_sessions(db: Session, user_id: int) -> None:
    db.query(models.AuthSession).filter(
        models.AuthSession.user_id == user_id, models.AuthSession.revoked_at.is_(None)
    ).update({models.AuthSession.revoked_at: datetime.now(timezone.utc)})


@router.get("/users", response_model=list[UserAdminOut])
def list_users(admin: Principal = Depends(_ADMIN), db: Session = Depends(get_db)) -> list[UserAdminOut]:
    rows = db.query(models.User).order_by(models.User.username).all()
    return [_out(u) for u in rows]


@router.post("/users", response_model=UserAdminOut, status_code=status.HTTP_201_CREATED)
def create_user(
    body: CreateUserIn, request: Request, admin: Principal = Depends(_ADMIN), db: Session = Depends(get_db)
) -> UserAdminOut:
    _validate_role(body.role)
    try:
        validate_password_strength(body.password)
        extra = normalize_extra_permissions(body.extra_permissions)
    except (WeakPasswordError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    user = models.User(
        username=body.username,
        email=body.email,
        password_hash=hash_password(body.password),
        role=body.role,
        extra_permissions=extra or None,
        must_change_password=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Username already taken: {body.username}") from None
    db.refresh(user)
    audit.record(
        db, actor=admin.actor, actor_label=admin.label, action=audit.USER_CREATED,
        target=f"user:{user.id}", ip=client_ip(request), user_agent=request.headers.get("user-agent"),
        detail={"username": user.username, "role": user.role, "extra_permissions": extra},
    )
    return _out(user)


@router.patch("/users/{user_id}", response_model=UserAdminOut)
def update_user(
    user_id: int, body: UpdateUserIn, request: Request,
    admin: Principal = Depends(_ADMIN), db: Session = Depends(get_db),
) -> UserAdminOut:
    user = db.query(models.User).filter(models.User.id == user_id).one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown user")

    changes: dict = {}
    if body.role is not None and body.role != user.role:
        _validate_role(body.role)
        if user.role == "admin" and body.role != "admin" and _active_admin_count(db, exclude_id=user.id) == 0:
            raise HTTPException(status.HTTP_409_CONFLICT, "Cannot demote the last active admin")
        changes["role"] = {"from": user.role, "to": body.role}
        user.role = body.role
    if body.is_active is not None and body.is_active != user.is_active:
        if not body.is_active:
            if user.id == admin.user_id:
                raise HTTPException(status.HTTP_409_CONFLICT, "You cannot deactivate your own account")
            if user.role == "admin" and _active_admin_count(db, exclude_id=user.id) == 0:
                raise HTTPException(status.HTTP_409_CONFLICT, "Cannot deactivate the last active admin")
            _revoke_sessions(db, user.id)
        changes["is_active"] = body.is_active
        user.is_active = body.is_active
    if body.email is not None:
        user.email = body.email or None
        changes["email"] = user.email
    if body.extra_permissions is not None:
        try:
            extra = normalize_extra_permissions(body.extra_permissions)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        user.extra_permissions = extra or None
        changes["extra_permissions"] = extra

    db.commit()
    db.refresh(user)
    audit.record(
        db, actor=admin.actor, actor_label=admin.label,
        action=audit.USER_DEACTIVATED if changes.get("is_active") is False else audit.USER_UPDATED,
        target=f"user:{user.id}", ip=client_ip(request), user_agent=request.headers.get("user-agent"),
        detail=changes,
    )
    return _out(user)


@router.post("/users/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(
    user_id: int, body: ResetPasswordIn, request: Request,
    admin: Principal = Depends(_ADMIN), db: Session = Depends(get_db),
) -> Response:
    user = db.query(models.User).filter(models.User.id == user_id).one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown user")
    try:
        validate_password_strength(body.new_password)
    except WeakPasswordError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    user.password_hash = hash_password(body.new_password)
    user.must_change_password = True
    _revoke_sessions(db, user.id)
    db.commit()
    audit.record(
        db, actor=admin.actor, actor_label=admin.label, action=audit.USER_PASSWORD_RESET,
        target=f"user:{user.id}", ip=client_ip(request), user_agent=request.headers.get("user-agent"),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_user(
    user_id: int, request: Request, admin: Principal = Depends(_ADMIN), db: Session = Depends(get_db)
) -> Response:
    user = db.query(models.User).filter(models.User.id == user_id).one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown user")
    if user.id == admin.user_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You cannot deactivate your own account")
    if user.role == "admin" and _active_admin_count(db, exclude_id=user.id) == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot deactivate the last active admin")

    user.is_active = False
    _revoke_sessions(db, user.id)
    db.commit()
    audit.record(
        db, actor=admin.actor, actor_label=admin.label, action=audit.USER_DEACTIVATED,
        target=f"user:{user.id}", ip=client_ip(request), user_agent=request.headers.get("user-agent"),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class AuditOut(BaseModel):
    id: int
    timestamp: datetime
    actor: str
    actor_label: str | None
    action: str
    target: str | None
    outcome: str
    ip: str | None
    detail: dict | None


@router.get("/audit", response_model=list[AuditOut])
def read_audit(
    admin: Principal = Depends(_ADMIN),
    db: Session = Depends(get_db),
    actor: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> list[AuditOut]:
    q = db.query(models.AuditLog)
    if actor:
        q = q.filter(models.AuditLog.actor == actor)
    if action:
        q = q.filter(models.AuditLog.action == action)
    if outcome:
        q = q.filter(models.AuditLog.outcome == outcome)
    if since:
        q = q.filter(models.AuditLog.timestamp >= since)
    rows = q.order_by(models.AuditLog.timestamp.desc()).limit(min(max(limit, 1), 1000)).all()
    return [
        AuditOut(
            id=r.id, timestamp=r.timestamp, actor=r.actor, actor_label=r.actor_label,
            action=r.action, target=r.target, outcome=r.outcome, ip=r.ip, detail=r.detail,
        )
        for r in rows
    ]
