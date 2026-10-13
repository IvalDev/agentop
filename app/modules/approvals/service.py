"""Approvals service: request → approve/reject → execute, bound to one exact call.

Product policy:

- Any user of the request's tenant whose role satisfies the request's stored
  ``required_role`` may read, approve, reject, or execute it — the requester
  included. A superadmin passes the role check, but the tenant must still
  match. Listing omits requests the caller's role does not satisfy; a get of
  one raises :class:`ApprovalForbiddenError`.
- Requests of other tenants are indistinguishable from missing ones
  (:class:`ApprovalNotFoundError`), which is checked before the role.
- The approval binds the exact tool + canonical input (decision A7):
  ``action_key`` is computed here with ``compute_action_key`` (never accepted
  from a caller); execution re-derives it from the stored payload, refuses on
  any mismatch, and runs exactly the stored payload.
- Requests expire ``Settings.approval_ttl_seconds`` after creation. Expiry
  is enforced on approve, reject, and execute: the first such call at or
  after ``expires_at`` marks a pending/approved request ``expired``. Listing
  reports the stored status; clients compare ``expires_at`` themselves.

Transactions: :func:`create_approval_request` only flushes (the caller
commits, like ``create_document``). Every other transition commits itself,
because its outcome — an ``expired`` mark or a single-use claim included —
must persist even when the caller raises afterwards.

Every transition is a conditional ``UPDATE ... WHERE id = ? AND status = ?``
whose rowcount picks the single winner, on SQLite and PostgreSQL alike. The
clock is injectable (``now``, timezone-aware) and defaults to UTC now.
"""

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from pydantic_core import to_jsonable_python
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.approvals.exceptions import (
    ApprovalBindingError,
    ApprovalExpiredError,
    ApprovalForbiddenError,
    ApprovalNotFoundError,
    ApprovalNotRequiredError,
    ApprovalStateError,
)
from app.modules.approvals.grants import mint_grant
from app.modules.approvals.models import ApprovalRequest, ApprovalStatus
from app.modules.identity.deps import TenantContext
from app.modules.identity.models import UserRole
from app.modules.identity.service import scoped_query
from app.modules.tools.exceptions import ToolNotFoundError
from app.modules.tools.executor import (
    ToolExecutionResult,
    compute_action_key,
    execute_tool,
    role_satisfies,
)
from app.modules.tools.registry import ApprovalPolicy, ToolRegistry, ToolSpec, ToolUser

#: Statuses a TTL can still expire.
_EXPIRABLE = (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)


@dataclass(frozen=True)
class ApprovalExecution:
    """Outcome of :func:`execute_approved_action`: the final row and tool result."""

    request: ApprovalRequest
    result: ToolExecutionResult


def _utc(now: datetime | None) -> datetime:
    """Normalize the injected clock value (default: current UTC time)."""
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


async def _load(session: AsyncSession, user: ToolUser, request_id: UUID) -> ApprovalRequest:
    """Fetch a fresh copy of a request in ``user``'s tenant, else not-found."""
    context = TenantContext(tenant_id=user.tenant_id, user_id=user.id, role=user.role)
    result = await session.execute(
        scoped_query(ApprovalRequest, context)
        .where(ApprovalRequest.id == request_id)
        .execution_options(populate_existing=True)
    )
    request = result.scalar_one_or_none()
    if request is None:
        raise ApprovalNotFoundError("approval request not found")
    return request


def _require_role(user: ToolUser, request: ApprovalRequest) -> None:
    if not role_satisfies(user, UserRole(request.required_role)):
        raise ApprovalForbiddenError("forbidden")


def _readable_roles(user: ToolUser) -> list[str]:
    """Stored ``required_role`` values ``user`` satisfies (every role for a superadmin)."""
    return [role.value for role in UserRole if role_satisfies(user, role)]


def _require_status(request: ApprovalRequest, expected: ApprovalStatus) -> None:
    if request.status is not expected:
        raise ApprovalStateError(f"approval request is {request.status.value}")


async def _transition(
    session: AsyncSession,
    request_id: UUID,
    expected: ApprovalStatus,
    values: dict[str, Any],
    *,
    alive_at: datetime | None = None,
) -> bool:
    """Apply ``values`` iff the row is still ``expected`` (and unexpired at ``alive_at``).

    The id was resolved by a tenant-scoped load. Returns whether this caller
    won the transition; in-memory objects are not synchronized (reload).
    """
    statement = (
        update(ApprovalRequest)
        .where(ApprovalRequest.id == request_id, ApprovalRequest.status == expected)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if alive_at is not None:
        statement = statement.where(ApprovalRequest.expires_at > alive_at)
    result = await session.execute(statement)
    return result.rowcount == 1


async def _expire_if_due(session: AsyncSession, request: ApprovalRequest, now: datetime) -> None:
    """Raise :class:`ApprovalExpiredError` for an expired request, marking it first."""
    if request.status is ApprovalStatus.EXPIRED:
        raise ApprovalExpiredError("approval request expired")
    if request.status in _EXPIRABLE and now >= request.expires_at:
        await _transition(session, request.id, request.status, {"status": ApprovalStatus.EXPIRED})
        await session.commit()
        raise ApprovalExpiredError("approval request expired")


def _verify_binding(registry: ToolRegistry, request: ApprovalRequest) -> ToolSpec:
    """Return the tool spec iff the stored call still hashes to the approved key.

    The stored payload must be the canonical validated input (no extra or
    coerced fields) and ``compute_action_key`` over it must equal the stored
    ``action_key``.
    """
    mismatch = ApprovalBindingError("approval binding mismatch")
    try:
        spec = registry.get(request.tool_name)
        assert spec.input_model is not None  # registration invariant
        validated = spec.input_model.model_validate(request.input_payload)
    except (ToolNotFoundError, ValidationError) as exc:
        raise mismatch from exc
    expected_key = compute_action_key(spec.name, validated)
    if validated.model_dump(mode="json") != request.input_payload or not hmac.compare_digest(
        expected_key, request.action_key
    ):
        raise mismatch
    return spec


async def create_approval_request(
    session: AsyncSession,
    registry: ToolRegistry,
    user: ToolUser,
    tool_name: str,
    raw_input: dict[str, Any],
    *,
    settings: Settings,
    now: datetime | None = None,
) -> ApprovalRequest:
    """Persist a pending request to run ``tool_name`` with ``raw_input`` (caller commits).

    Raises:
        ToolNotFoundError: unknown tool.
        pydantic.ValidationError: ``raw_input`` fails the tool's input model.
        ApprovalNotRequiredError: the tool is not ``REQUIRE_APPROVAL``.
        ApprovalForbiddenError: ``user`` may not invoke the tool at all.
    """
    now = _utc(now)
    spec = registry.get(tool_name)
    if spec.approval_policy is not ApprovalPolicy.REQUIRE_APPROVAL:
        raise ApprovalNotRequiredError(f"tool {tool_name!r} does not require approval")
    if not role_satisfies(user, spec.required_role):
        raise ApprovalForbiddenError("forbidden")
    assert spec.input_model is not None  # registration invariant
    validated = spec.input_model.model_validate(raw_input)
    request = ApprovalRequest(
        tenant_id=user.tenant_id,
        requested_by=user.id,
        tool_name=spec.name,
        input_payload=validated.model_dump(mode="json"),
        action_key=compute_action_key(spec.name, validated),
        required_role=spec.required_role.value,
        status=ApprovalStatus.PENDING,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.approval_ttl_seconds),
    )
    session.add(request)
    await session.flush()
    return request


async def list_approval_requests(
    session: AsyncSession, user: ToolUser, status: ApprovalStatus | None = None
) -> list[ApprovalRequest]:
    """Return the tenant's requests ``user``'s role may read, newest first.

    The role filter runs in the query (``required_role IN`` the satisfied
    roles), so any status filter, limit, or count applies to readable rows only.
    """
    context = TenantContext(tenant_id=user.tenant_id, user_id=user.id, role=user.role)
    query = (
        scoped_query(ApprovalRequest, context)
        .where(ApprovalRequest.required_role.in_(_readable_roles(user)))
        .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id)
    )
    if status is not None:
        query = query.where(ApprovalRequest.status == status)
    return list((await session.execute(query)).scalars())


async def get_approval_request(
    session: AsyncSession, user: ToolUser, request_id: UUID
) -> ApprovalRequest:
    """Return one request of ``user``'s tenant that ``user``'s role may read.

    Raises :class:`ApprovalNotFoundError` for foreign or unknown ids, then
    :class:`ApprovalForbiddenError` when the role does not satisfy the
    request's stored ``required_role``.
    """
    request = await _load(session, user, request_id)
    _require_role(user, request)
    return request


async def _decide(
    session: AsyncSession,
    user: ToolUser,
    request_id: UUID,
    outcome: ApprovalStatus,
    reason: str | None,
    now: datetime | None,
) -> ApprovalRequest:
    """One-shot pending → ``outcome`` decision (see :func:`approve_request`)."""
    now = _utc(now)
    request = await _load(session, user, request_id)
    _require_role(user, request)
    await _expire_if_due(session, request, now)
    _require_status(request, ApprovalStatus.PENDING)
    decision = {
        "status": outcome,
        "decided_by": user.id,
        "decided_at": now,
        "decision_reason": reason,
    }
    if not await _transition(session, request_id, ApprovalStatus.PENDING, decision, alive_at=now):
        await session.rollback()
        raise ApprovalStateError("approval request was already decided")
    await session.commit()
    return await _load(session, user, request_id)


async def approve_request(
    session: AsyncSession, user: ToolUser, request_id: UUID, *, now: datetime | None = None
) -> ApprovalRequest:
    """Approve a pending request (commits).

    Raises, in check order: :class:`ApprovalNotFoundError`,
    :class:`ApprovalForbiddenError`, :class:`ApprovalExpiredError` (after
    marking the request expired), :class:`ApprovalStateError` (already
    decided, including a lost race).
    """
    return await _decide(session, user, request_id, ApprovalStatus.APPROVED, None, now)


async def reject_request(
    session: AsyncSession,
    user: ToolUser,
    request_id: UUID,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> ApprovalRequest:
    """Reject a pending request with an optional reason (same rules as approve)."""
    return await _decide(session, user, request_id, ApprovalStatus.REJECTED, reason, now)


async def execute_approved_action(
    session: AsyncSession,
    registry: ToolRegistry,
    user: ToolUser,
    request_id: UUID,
    *,
    now: datetime | None = None,
) -> ApprovalExecution:
    """Run an approved request's stored call at most once (commits).

    Check order: tenant-scoped load (not-found) → stored required role
    (forbidden) → TTL (expired, marked) → status ``approved`` (state) →
    binding (a mismatch marks the request ``failed`` and raises
    :class:`ApprovalBindingError`) → the tool's *current* required role
    (forbidden). Every refusal above leaves a valid approval usable.

    Then an atomic ``approved → executing`` claim is committed (a loser of a
    race gets :class:`ApprovalStateError`), ``execute_tool`` runs the stored
    payload with a freshly minted single-use grant, and the request ends
    ``executed`` (with the JSON-encoded output) or ``failed`` (with the error;
    the handler's uncommitted writes are rolled back first). A crash after
    the claim leaves ``executing``: never re-runnable, visible for operators.
    """
    now = _utc(now)
    request = await _load(session, user, request_id)
    _require_role(user, request)
    await _expire_if_due(session, request, now)
    _require_status(request, ApprovalStatus.APPROVED)
    try:
        spec = _verify_binding(registry, request)
    except ApprovalBindingError as exc:
        failure = {"status": ApprovalStatus.FAILED, "execution_error": str(exc)}
        await _transition(session, request_id, ApprovalStatus.APPROVED, failure)
        await session.commit()
        raise
    if not role_satisfies(user, spec.required_role):
        raise ApprovalForbiddenError("forbidden")

    payload, action_key = dict(request.input_payload), request.action_key
    claim = {"status": ApprovalStatus.EXECUTING, "executed_by": user.id, "executed_at": now}
    if not await _transition(session, request_id, ApprovalStatus.APPROVED, claim, alive_at=now):
        await session.rollback()
        raise ApprovalStateError("approval request was already executed")
    await session.commit()

    result = await execute_tool(
        registry,
        session,
        user,
        spec.name,
        payload,
        approval_grant=mint_grant(request_id, action_key),
    )
    if result.status == "ok":
        outcome = {
            "status": ApprovalStatus.EXECUTED,
            "execution_result": to_jsonable_python(result.output, fallback=str),
        }
    else:
        await session.rollback()
        outcome = {"status": ApprovalStatus.FAILED, "execution_error": result.error}
    await _transition(session, request_id, ApprovalStatus.EXECUTING, outcome)
    await session.commit()
    return ApprovalExecution(request=await _load(session, user, request_id), result=result)
