from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_user
from app.db.models import AuditLog, Employee, InventoryItem, User
from app.db.session import get_db

router = APIRouter()

DbDep = Annotated[AsyncSession, Depends(get_db)]

RoleName = Literal["hr", "candidate", "admin"]


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


async def _append_audit(
    db: AsyncSession,
    *,
    actor: dict,
    action: str,
    resource: str,
    detail: str,
) -> None:
    """Insert an audit row. Caller commits."""
    db.add(
        AuditLog(
            id=f"aud-{uuid.uuid4().hex[:10]}",
            timestamp=_now(),
            actor_email=str(actor.get("email", "unknown")),
            action=action,
            resource=resource,
            detail=detail,
        )
    )


class EmployeeOut(BaseModel):
    employee_id: str
    user_id: str | None
    full_name: str
    title: str
    department: str
    status: str
    start_date: str


@router.get("/admin/employees", response_model=list[EmployeeOut])
async def list_employees(
    db: DbDep,
    _payload: dict = Depends(admin_user),
) -> list[EmployeeOut]:
    result = await db.execute(select(Employee).order_by(Employee.full_name))
    employees = result.scalars().all()
    return [
        EmployeeOut(
            employee_id=e.id,
            user_id=e.user_id,
            full_name=e.full_name,
            title=e.title,
            department=e.department,
            status=e.status,
            start_date=e.start_date.isoformat() if e.start_date else "",
        )
        for e in employees
    ]


class InventoryRow(BaseModel):
    sku: str
    name: str
    category: str
    in_stock: int
    reserved: int
    reorder_point: int


@router.get("/admin/assets/inventory", response_model=list[InventoryRow])
async def asset_inventory(
    db: DbDep,
    _payload: dict = Depends(admin_user),
) -> list[InventoryRow]:
    result = await db.execute(select(InventoryItem).order_by(InventoryItem.sku))
    items = result.scalars().all()
    return [
        InventoryRow(
            sku=i.sku,
            name=i.name,
            category=i.category,
            in_stock=i.in_stock,
            reserved=i.reserved,
            reorder_point=i.reorder_point,
        )
        for i in items
    ]


class AuditLogOut(BaseModel):
    id: str
    timestamp: str
    actor_email: str
    action: str
    resource: str
    detail: str


@router.get("/admin/audit-logs", response_model=list[AuditLogOut])
async def audit_logs(
    db: DbDep,
    _payload: dict = Depends(admin_user),
    limit: int = 50,
) -> list[AuditLogOut]:
    # Newest first, capped at `limit`.
    result = await db.execute(
        select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit)
    )
    rows = result.scalars().all()
    return [
        AuditLogOut(
            id=a.id,
            timestamp=a.timestamp.isoformat(),
            actor_email=a.actor_email,
            action=a.action,
            resource=a.resource,
            detail=a.detail,
        )
        for a in rows
    ]


class AdminUserOut(BaseModel):
    user_id: str
    email: str
    display_name: str
    role: str


@router.get("/admin/users", response_model=list[AdminUserOut])
async def list_users(
    db: DbDep,
    _payload: dict = Depends(admin_user),
) -> list[AdminUserOut]:
    result = await db.execute(select(User).order_by(User.email))
    users = result.scalars().all()
    return [
        AdminUserOut(
            user_id=u.id,
            email=u.email,
            display_name=u.display_name,
            role=u.role,
        )
        for u in users
    ]


class PatchRoleBody(BaseModel):
    role: RoleName


class PatchRoleResponse(BaseModel):
    user_id: str
    role: str
    notice: str


@router.patch(
    "/admin/users/{user_id}/role",
    response_model=PatchRoleResponse,
)
async def patch_user_role(
    user_id: str,
    body: PatchRoleBody,
    db: DbDep,
    payload: dict = Depends(admin_user),
) -> PatchRoleResponse:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")

    prev = user.role
    user.role = body.role
    await _append_audit(
        db,
        actor=payload,
        action="admin.role_change",
        resource=user_id,
        detail=f"role {prev} -> {body.role}",
    )
    await db.commit()

    return PatchRoleResponse(
        user_id=user_id,
        role=body.role,
        # The role lives in the signed JWT, so an already-issued token keeps its
        # old role until the user signs in again and gets a fresh token.
        notice="Directory role updated. JWT role changes after the user signs in again.",
    )
