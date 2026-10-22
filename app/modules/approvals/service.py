"""Approvals service: request → approve/reject → execute, bound to one exact call.

Product policy:

- Any user of the request's tenant whose role satisfies the request's stored
  ``required_role`` may read, approve, reject, or execute it — the requester
  included. A superadmin passes the role check, but the tenant must still
  match. Listing omits requests the caller's role does not satisfy; a get of
  one raises :class:`ApprovalForbiddenError`.
- Requester-only tools (``ToolSpec.requester_only_approval``, google-mail
  G7): a tool acting on the requester's own account — a per-user provider
  connection such as their Gmail mailbox — runs with the identity of whoever
  executes it, so only ``requested_by`` may approve, reject, or execute its
  requests; anyone else, a superadmin or a higher role included, gets
  :class:`ApprovalForbiddenError` (audited ``approval.denied`` with reason
  ``requester_only``), checked right after the role check, which the
  requester must still pass. Reads and listing keep the role rule. The flag
  is read from the registry (a tool missing from it fails closed:
  requester only); a decision made without a ``registry`` cannot read it
  and applies the role rule alone — the HTTP routes always pass one, and
  execution always has one.
- Requests of other tenants are indistinguishable from missing ones
  (:class:`ApprovalNotFoundError`), which is checked before the role.
- Who may *create* a request is the tool's invocation role
  (``ToolSpec.required_role``). The stored ``required_role`` is the approver
  role of that exact call: :func:`~app.modules.tools.executor.resolve_approval_role`
  — the tool's role unless its ``approval_role`` resolver asks for a more
  privileged one (e.g. by amount). Every read and decision uses the stored
  value; a resolver that fails stores nothing.
- The approval binds the exact tool + canonical input (decision A7):
  ``action_key`` is computed here with ``compute_action_key`` (never accepted
  from a caller); execution re-derives it and the approver role from the
  stored payload, refuses on any mismatch (a tampered ``required_role`` is
  caught like a tampered payload), and runs exactly the stored payload.
- Execution runs the tool's precheck again right before its handler (inside
  ``execute_tool``); a refusal is a failed run like a handler error: the
  request ends ``failed`` with ``precheck_failed: <reason>``. The tool's
  module must still be active for the tenant at execution time (the set is
  re-read with ``active_module_keys``): a module deactivated between
  approval and execution ends the request ``failed`` with
  ``inactive_module``.
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

Audit (T8): each transition stages its event in the same transaction that
commits it — ``approval.created`` (with the caller's commit),
``approval.approved``/``rejected``, ``approval.expired`` (actor ``system``;
the triggering user is ``payload.detected_by``), ``approval.binding_mismatch``,
and ``approval.executed``/``failed`` — so an event exists iff its transition
does; losers of a race record nothing. A refused create/approve/reject/execute
is persisted at once as ``approval.denied`` (no state changes, so it must not
depend on the caller's transaction). Every event of a request carries its id
as ``resource_id`` and ``correlation_id``; the tool run passes the same
``correlation_id`` to the executor's ``tool.*`` event. The one exception is
``approval.created``, whose actor type and correlation a caller may set: the
agent loop records it as ``agent`` with its run id, linking the request to
the run that submitted it.
"""

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn
from uuid import UUID

from pydantic import ValidationError
from pydantic_core import to_jsonable_python
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.agent.llm import LLMProvider
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
from app.modules.audit.models import ActorType
from app.modules.audit.service import persist_event, stage_event
from app.modules.catalog.service import active_module_keys
from app.modules.connectors.hub import ConnectorRequester
from app.modules.documents.storage import DocumentStorage
from app.modules.identity.deps import TenantContext
from app.modules.identity.models import UserRole
from app.modules.identity.service import scoped_query
from app.modules.tools.exceptions import ToolApprovalRoleError, ToolNotFoundError
from app.modules.tools.executor import (
    ToolExecutionResult,
    compute_action_key,
    execute_tool,
    resolve_approval_role,
    role_satisfies,
)
from app.modules.tools.registry import ApprovalPolicy, ToolRegistry, ToolSpec, ToolUser

#: Statuses a TTL can still expire.
_EXPIRABLE = (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
_RESOURCE = "approval_request"


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


def _stage(
    session: AsyncSession,
    action: str,
    *,
    tenant_id: int,
    request_id: UUID,
    tool_name: str,
    actor: ToolUser | None,
    payload: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    actor_type: ActorType | None = None,
    correlation_id: str | None = None,
) -> None:
    """Stage a lifecycle event of one request (commits with its transition).

    ``actor_type`` defaults to the audit service's rule (``user`` with an
    actor, else ``system``) and ``correlation_id`` to the request id.
    """
    stage_event(
        session,
        tenant_id=tenant_id,
        action=action,
        actor_user_id=None if actor is None else actor.id,
        actor_type=actor_type,
        resource_type=_RESOURCE,
        resource_id=str(request_id),
        tool_name=tool_name,
        payload=payload,
        result=result,
        correlation_id=str(request_id) if correlation_id is None else correlation_id,
    )


async def _deny(
    session: AsyncSession,
    user: ToolUser,
    operation: str,
    tool_name: str,
    request_id: UUID | None = None,
    *,
    reason: str | None = None,
) -> NoReturn:
    """Persist ``approval.denied`` for ``operation`` (with ``reason``, if any), then raise."""
    reference = None if request_id is None else str(request_id)
    result = {"error": "forbidden"} if reason is None else {"error": "forbidden", "reason": reason}
    await persist_event(
        session,
        tenant_id=user.tenant_id,
        action="approval.denied",
        actor_user_id=user.id,
        resource_type=_RESOURCE,
        resource_id=reference,
        tool_name=tool_name,
        payload={"operation": operation},
        result=result,
        correlation_id=reference,
    )
    raise ApprovalForbiddenError("forbidden")


def _requester_only(registry: ToolRegistry | None, tool_name: str) -> bool:
    """Whether only the requester may decide or execute ``tool_name``'s requests.

    ``None`` (no registry given) cannot tell and keeps the role rule alone; a
    tool the registry no longer knows fails closed (requester only).
    """
    if registry is None:
        return False
    try:
        return registry.get(tool_name).requester_only_approval
    except ToolNotFoundError:
        return True


async def _authorize(
    session: AsyncSession,
    user: ToolUser,
    request: ApprovalRequest,
    operation: str,
    *,
    registry: ToolRegistry | None,
) -> None:
    """Audited :func:`_require_role`, then the requester-only rule, for state changes."""
    if not role_satisfies(user, UserRole(request.required_role)):
        await _deny(session, user, operation, request.tool_name, request.id)
    if _requester_only(registry, request.tool_name) and user.id != request.requested_by:
        await _deny(
            session, user, operation, request.tool_name, request.id, reason="requester_only"
        )


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


async def _expire_if_due(
    session: AsyncSession, user: ToolUser, request: ApprovalRequest, now: datetime
) -> None:
    """Raise :class:`ApprovalExpiredError` for an expired request, marking it first.

    The winner of the ``expired`` transition stages a ``system`` event naming
    ``user`` as the caller that detected the expiry.
    """
    if request.status is ApprovalStatus.EXPIRED:
        raise ApprovalExpiredError("approval request expired")
    if request.status in _EXPIRABLE and now >= request.expires_at:
        expired = {"status": ApprovalStatus.EXPIRED}
        if await _transition(session, request.id, request.status, expired):
            _stage(
                session,
                "approval.expired",
                tenant_id=request.tenant_id,
                request_id=request.id,
                tool_name=request.tool_name,
                actor=None,
                payload={
                    "detected_by": user.id,
                    "previous_status": request.status.value,
                    "expires_at": request.expires_at.isoformat(),
                },
            )
        await session.commit()
        raise ApprovalExpiredError("approval request expired")


def _verify_binding(registry: ToolRegistry, request: ApprovalRequest) -> ToolSpec:
    """Return the tool spec iff the stored call still hashes to the approved key.

    The stored payload must be the canonical validated input (no extra or
    coerced fields), ``compute_action_key`` over it must equal the stored
    ``action_key``, and the approver role re-derived from it must equal the
    stored ``required_role`` (a resolver that now fails cannot confirm it).
    """
    mismatch = ApprovalBindingError("approval binding mismatch")
    try:
        spec = registry.get(request.tool_name)
        assert spec.input_model is not None  # registration invariant
        validated = spec.input_model.model_validate(request.input_payload)
        approver = resolve_approval_role(spec, validated)
    except (ToolNotFoundError, ValidationError, ToolApprovalRoleError) as exc:
        raise mismatch from exc
    expected_key = compute_action_key(spec.name, validated)
    if (
        validated.model_dump(mode="json") != request.input_payload
        or not hmac.compare_digest(expected_key, request.action_key)
        or approver.value != request.required_role
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
    actor_type: ActorType = ActorType.USER,
    correlation_id: str | None = None,
) -> ApprovalRequest:
    """Persist a pending request to run ``tool_name`` with ``raw_input`` (caller commits).

    ``user`` is always the requester and must satisfy the tool's invocation
    role; the stored ``required_role`` is the call's resolved approver role.
    ``actor_type`` and ``correlation_id`` (default: the new request's id)
    apply to the ``approval.created`` event only; every later event of the
    request is correlated by its id.

    Raises:
        ToolNotFoundError: unknown tool.
        pydantic.ValidationError: ``raw_input`` fails the tool's input model.
        ApprovalNotRequiredError: the tool is not ``REQUIRE_APPROVAL``.
        ApprovalForbiddenError: ``user`` may not invoke the tool at all.
        ToolApprovalRoleError: the tool's approver-role resolver failed.
    """
    now = _utc(now)
    spec = registry.get(tool_name)
    if spec.approval_policy is not ApprovalPolicy.REQUIRE_APPROVAL:
        raise ApprovalNotRequiredError(f"tool {tool_name!r} does not require approval")
    if not role_satisfies(user, spec.required_role):
        await _deny(session, user, "create", spec.name)
    assert spec.input_model is not None  # registration invariant
    validated = spec.input_model.model_validate(raw_input)
    approver = resolve_approval_role(spec, validated)
    request = ApprovalRequest(
        tenant_id=user.tenant_id,
        requested_by=user.id,
        tool_name=spec.name,
        input_payload=validated.model_dump(mode="json"),
        action_key=compute_action_key(spec.name, validated),
        required_role=approver.value,
        status=ApprovalStatus.PENDING,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.approval_ttl_seconds),
    )
    session.add(request)
    await session.flush()
    _stage(
        session,
        "approval.created",
        tenant_id=request.tenant_id,
        request_id=request.id,
        tool_name=spec.name,
        actor=user,
        payload=request.input_payload,
        result={
            "action_key": request.action_key,
            "required_role": request.required_role,
            "expires_at": request.expires_at.isoformat(),
        },
        actor_type=actor_type,
        correlation_id=correlation_id,
    )
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
    registry: ToolRegistry | None,
) -> ApprovalRequest:
    """One-shot pending → ``outcome`` decision (see :func:`approve_request`)."""
    now = _utc(now)
    request = await _load(session, user, request_id)
    operation = "approve" if outcome is ApprovalStatus.APPROVED else "reject"
    await _authorize(session, user, request, operation, registry=registry)
    await _expire_if_due(session, user, request, now)
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
    _stage(
        session,
        f"approval.{outcome.value}",
        tenant_id=request.tenant_id,
        request_id=request_id,
        tool_name=request.tool_name,
        actor=user,
        payload=None if reason is None else {"reason": reason},
    )
    await session.commit()
    return await _load(session, user, request_id)


async def approve_request(
    session: AsyncSession,
    user: ToolUser,
    request_id: UUID,
    *,
    now: datetime | None = None,
    registry: ToolRegistry | None = None,
) -> ApprovalRequest:
    """Approve a pending request (commits).

    Raises, in check order: :class:`ApprovalNotFoundError`,
    :class:`ApprovalForbiddenError` (role below the stored one, or — with
    ``registry`` — not the requester of a requester-only tool),
    :class:`ApprovalExpiredError` (after marking the request expired),
    :class:`ApprovalStateError` (already decided, including a lost race).
    """
    return await _decide(session, user, request_id, ApprovalStatus.APPROVED, None, now, registry)


async def reject_request(
    session: AsyncSession,
    user: ToolUser,
    request_id: UUID,
    *,
    reason: str | None = None,
    now: datetime | None = None,
    registry: ToolRegistry | None = None,
) -> ApprovalRequest:
    """Reject a pending request with an optional reason (same rules as approve)."""
    return await _decide(session, user, request_id, ApprovalStatus.REJECTED, reason, now, registry)


async def execute_approved_action(
    session: AsyncSession,
    registry: ToolRegistry,
    user: ToolUser,
    request_id: UUID,
    *,
    now: datetime | None = None,
    llm: LLMProvider | None = None,
    storage: DocumentStorage | None = None,
    credentials_key: str | None = None,
    settings: Settings | None = None,
    connectors: ConnectorRequester | None = None,
) -> ApprovalExecution:
    """Run an approved request's stored call at most once (commits).

    Check order: tenant-scoped load (not-found) → stored required role
    (forbidden) → requester-only tool executed by someone other than its
    requester (forbidden) → TTL (expired, marked) → status ``approved`` (state) →
    binding, approver role included (a mismatch marks the request ``failed``
    and raises :class:`ApprovalBindingError`) → the tool's *current* required
    role (forbidden). Every refusal above leaves a valid approval usable.

    Then an atomic ``approved → executing`` claim is committed (a loser of a
    race gets :class:`ApprovalStateError`), ``execute_tool`` runs the stored
    payload with a freshly minted single-use grant (and ``llm``/``storage``
    plus ``credentials_key``/``settings`` and the ``connectors`` hub in the
    tool context — without the key, a tool decrypting per-user provider
    credentials fails its precheck closed, and without the hub a local-folder
    tool fails its precheck offline) gated by the tenant's *current* module
    activations
    (re-read with ``active_module_keys`` once per execution: a tool of a
    module deactivated since the approval is refused there as
    ``inactive_module``), and the request ends ``executed`` (with the
    JSON-encoded output) or ``failed`` (with the error — a precheck refusal
    or an inactive module included; the handler's uncommitted writes are
    rolled back first). A crash after the claim leaves ``executing``: never
    re-runnable, visible for operators.
    """
    now = _utc(now)
    request = await _load(session, user, request_id)
    await _authorize(session, user, request, "execute", registry=registry)
    await _expire_if_due(session, user, request, now)
    _require_status(request, ApprovalStatus.APPROVED)
    tenant_id, stored_tool = request.tenant_id, request.tool_name
    try:
        spec = _verify_binding(registry, request)
    except ApprovalBindingError as exc:
        failure = {"status": ApprovalStatus.FAILED, "execution_error": str(exc)}
        if await _transition(session, request_id, ApprovalStatus.APPROVED, failure):
            _stage(
                session,
                "approval.binding_mismatch",
                tenant_id=tenant_id,
                request_id=request_id,
                tool_name=stored_tool,
                actor=user,
                result={"error": str(exc)},
            )
        await session.commit()
        raise
    if not role_satisfies(user, spec.required_role):
        await _deny(session, user, "execute", spec.name, request_id)

    # Module activation may change between approval and execution: re-read it.
    active_modules = await active_module_keys(session, user.tenant_id)

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
        correlation_id=str(request_id),
        llm=llm,
        storage=storage,
        active_modules=active_modules,
        credentials_key=credentials_key,
        settings=settings,
        connectors=connectors,
    )
    if result.status == "ok":
        outcome = {
            "status": ApprovalStatus.EXECUTED,
            "execution_result": to_jsonable_python(result.output, fallback=str),
        }
        audit_action, audit_result = "approval.executed", {"status": "executed"}
    else:
        await session.rollback()
        outcome = {"status": ApprovalStatus.FAILED, "execution_error": result.error}
        audit_action, audit_result = "approval.failed", {"error": result.error}
    _stage(
        session,
        audit_action,
        tenant_id=tenant_id,
        request_id=request_id,
        tool_name=spec.name,
        actor=user,
        result=audit_result,
    )
    await _transition(session, request_id, ApprovalStatus.EXECUTING, outcome)
    await session.commit()
    return ApprovalExecution(request=await _load(session, user, request_id), result=result)
