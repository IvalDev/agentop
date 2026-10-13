"""Approvals API: review queue, one-shot decisions, and single-use execution.

Requests are created by the agent through the service (there is no HTTP
create endpoint), and the client never supplies a payload, action key, or
grant: execution always runs the stored, server-bound call. The tenant
always comes from the authenticated user.

HTTP mapping (one place, :data:`_HTTP_STATUS`):

====================================== ====================================
service error                           HTTP
====================================== ====================================
``ApprovalNotFoundError``               404 (unknown *and* other-tenant ids)
``ApprovalForbiddenError``              403 (role below the tool's requirement)
``ApprovalStateError``                  409 (already decided/executed, not approved)
``ApprovalBindingError``                409 (stored call no longer matches its key)
``ApprovalExpiredError``                410 Gone (TTL elapsed; request is ``expired``)
====================================== ====================================

Reads are open to every authenticated role of the tenant; approve, reject,
and execute need a role satisfying the tool's required role (requester
included). A tool that ran but failed still answers 200 with status
``failed`` and ``execution_error`` — the approval was consumed either way.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.modules.approvals.exceptions import (
    ApprovalBindingError,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalForbiddenError,
    ApprovalNotFoundError,
    ApprovalStateError,
)
from app.modules.approvals.models import ApprovalRequest, ApprovalStatus
from app.modules.approvals.service import (
    approve_request,
    execute_approved_action,
    get_approval_request,
    list_approval_requests,
    reject_request,
)
from app.modules.identity.deps import CurrentUser, get_current_user
from app.modules.tools.registry import ToolRegistry
from packages.contracts.schemas import (
    ApprovalListResponse,
    ApprovalRejectRequest,
    ApprovalResponse,
)

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def get_tool_registry(request: Request) -> ToolRegistry:
    """Return the tool registry attached to ``app.state.tool_registry``."""
    return request.app.state.tool_registry


ToolRegistryDep = Annotated[ToolRegistry, Depends(get_tool_registry)]

_HTTP_STATUS: dict[type[ApprovalError], tuple[int, str]] = {
    ApprovalNotFoundError: (404, "approval request not found"),
    ApprovalForbiddenError: (403, "forbidden"),
    ApprovalStateError: (409, "approval request is not in a valid state for this action"),
    ApprovalBindingError: (409, "approval binding mismatch"),
    ApprovalExpiredError: (410, "approval request expired"),
}


def _http_error(exc: ApprovalError) -> HTTPException:
    """Translate a service error; unmapped errors are server bugs (500)."""
    status_code, detail = _HTTP_STATUS.get(type(exc), (500, "internal error"))
    if isinstance(exc, ApprovalStateError):
        detail = str(exc)
    return HTTPException(status_code=status_code, detail=detail)


def _response(request: ApprovalRequest) -> ApprovalResponse:
    """Public view of a request (never exposes ``tenant_id``)."""
    return ApprovalResponse(
        id=request.id,
        tool_name=request.tool_name,
        input=request.input_payload,
        action_key=request.action_key,
        required_role=request.required_role,
        status=request.status.value,
        requested_by=request.requested_by,
        created_at=request.created_at,
        expires_at=request.expires_at,
        decided_by=request.decided_by,
        decided_at=request.decided_at,
        decision_reason=request.decision_reason,
        executed_by=request.executed_by,
        executed_at=request.executed_at,
        execution_result=request.execution_result,
        execution_error=request.execution_error,
    )


@router.get("", response_model=ApprovalListResponse)
async def list_approvals(
    session: SessionDep, user: CurrentUserDep, status: ApprovalStatus | None = None
) -> ApprovalListResponse:
    """Return the caller's tenant requests, newest first (any role)."""
    requests = await list_approval_requests(session, user, status)
    return ApprovalListResponse(approvals=[_response(request) for request in requests])


@router.get("/{approval_id}", response_model=ApprovalResponse)
async def get_approval(
    approval_id: UUID, session: SessionDep, user: CurrentUserDep
) -> ApprovalResponse:
    """Return one request of the caller's tenant (any role); 404 otherwise."""
    try:
        return _response(await get_approval_request(session, user, approval_id))
    except ApprovalError as exc:
        raise _http_error(exc) from exc


@router.post("/{approval_id}/approve", response_model=ApprovalResponse)
async def approve(approval_id: UUID, session: SessionDep, user: CurrentUserDep) -> ApprovalResponse:
    """Approve a pending request (role satisfying the tool's requirement)."""
    try:
        return _response(await approve_request(session, user, approval_id))
    except ApprovalError as exc:
        raise _http_error(exc) from exc


@router.post("/{approval_id}/reject", response_model=ApprovalResponse)
async def reject(
    approval_id: UUID,
    session: SessionDep,
    user: CurrentUserDep,
    body: ApprovalRejectRequest | None = None,
) -> ApprovalResponse:
    """Reject a pending request with an optional reason (same roles as approve)."""
    reason = body.reason if body is not None else None
    try:
        return _response(await reject_request(session, user, approval_id, reason=reason))
    except ApprovalError as exc:
        raise _http_error(exc) from exc


@router.post("/{approval_id}/execute", response_model=ApprovalResponse)
async def execute(
    approval_id: UUID,
    session: SessionDep,
    registry: ToolRegistryDep,
    user: CurrentUserDep,
) -> ApprovalResponse:
    """Run an approved request's stored call exactly once."""
    try:
        execution = await execute_approved_action(session, registry, user, approval_id)
    except ApprovalError as exc:
        raise _http_error(exc) from exc
    return _response(execution.request)
