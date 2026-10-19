"""Audit API: read-only, tenant-scoped trail for owners and admins.

There are no write, update, or delete routes — events are recorded by the
services (see :mod:`app.modules.audit.service`). ``GET /api/v1/audit``
requires ``owner`` or ``admin`` (a superadmin passes the role check, but the
tenant still comes from the authenticated user); other roles get 403.

Filters combine with AND: ``actor_user_id``, ``action`` (exact) or
``action_prefix`` (literal prefix such as ``tool.``), ``tool_name``,
``resource_type``/``resource_id``, ``correlation_id``, and the
``[created_from, created_to)`` window, whose bounds must carry a UTC offset.
Results are newest first; ``limit`` is 1–200 and ``next_cursor`` pages on.
A malformed cursor or a naive datetime is 422.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.modules.audit.models import AuditEvent
from app.modules.audit.service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    AuditFilters,
    InvalidCursorError,
    list_events,
)
from app.modules.identity.deps import CurrentUser, TenantContext, require_roles
from app.modules.identity.models import UserRole
from packages.contracts.schemas import AuditEventListResponse, AuditEventResponse

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
AuditorDep = Annotated[CurrentUser, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))]
ShortText = Annotated[str | None, Query(max_length=64)]


def _response(event: AuditEvent) -> AuditEventResponse:
    """Public view of an event (never exposes ``tenant_id``)."""
    return AuditEventResponse(
        id=event.id,
        action=event.action,
        actor_type=event.actor_type.value,
        actor_user_id=event.actor_user_id,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        tool_name=event.tool_name,
        payload=event.payload,
        result=event.result,
        correlation_id=event.correlation_id,
        created_at=event.created_at,
    )


@router.get("", response_model=AuditEventListResponse)
async def list_audit_events(
    session: SessionDep,
    user: AuditorDep,
    actor_user_id: int | None = None,
    action: ShortText = None,
    action_prefix: ShortText = None,
    tool_name: ShortText = None,
    resource_type: ShortText = None,
    resource_id: ShortText = None,
    correlation_id: ShortText = None,
    created_from: AwareDatetime | None = None,
    created_to: AwareDatetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> AuditEventListResponse:
    """Return one page of the caller's tenant audit trail, newest first."""
    filters = AuditFilters(
        actor_user_id=actor_user_id,
        action=action,
        action_prefix=action_prefix,
        tool_name=tool_name,
        resource_type=resource_type,
        resource_id=resource_id,
        correlation_id=correlation_id,
        created_from=created_from,
        created_to=created_to,
    )
    context = TenantContext(tenant_id=user.tenant_id, user_id=user.id, role=user.role)
    try:
        events, next_cursor = await list_events(
            session, context, filters, limit=limit, cursor=cursor
        )
    except InvalidCursorError as exc:
        raise HTTPException(status_code=422, detail="invalid cursor") from exc
    return AuditEventListResponse(
        events=[_response(event) for event in events], next_cursor=next_cursor
    )
