"""Bounded tool execution: lookup → authorize → validate → gate → run.

``execute_tool`` never raises for tool-level problems — every failure mode
comes back as a :class:`ToolExecutionResult` so a caller (the future tools
API or the T9 orchestrator) maps statuses in exactly one place. Suggested
HTTP mapping for an API layer:

============================ ==================== ==================
result                        HTTP                 ``error_kind``
============================ ==================== ==================
``ok`` / ``dry_run``          200                  —
``approval_required``         202                  —
``error`` (validation)        400                  ``validation``
``error`` (unknown tool)      400                  ``not_found``
``error`` (handler/output)    400                  ``execution``
``error`` (insufficient role) 403                  ``forbidden``
``error`` (inactive module)   403                  ``inactive_module``
``error`` (rejected grant)    403                  ``forbidden``
``error`` (precheck refused)  409                  ``precheck``
============================ ==================== ==================

Error messages are safe to show to API clients and to the model: a handler
exception — or an unexpected exception from a precheck or an approver-role
resolver — is reported as :data:`TOOL_FAILED` and an output-schema violation
as :data:`INVALID_OUTPUT` (the detail goes to the server log and the
``tool.failed`` audit event only), validation errors list field locations
and messages without echoing input values, and a precheck refusal is
``precheck_failed: <reason>`` with the tool's own user-safe reason
(:class:`~app.modules.tools.exceptions.ToolPrecheckError`).

Role checks compare an explicit rank map (lower = more privileged), mirroring
the declaration order of :class:`~app.modules.identity.models.UserRole`:
``owner=0, admin=1, operator=2, viewer=3``. A user may invoke a tool when
``rank(user.role) <= rank(spec.required_role)`` or ``user.is_superadmin``
(the superadmin bypass mirrors :func:`app.modules.identity.deps.require_roles`).

Approval gate (decision A7: the model never authorizes)
------------------------------------------------------
- ``approval_grant`` — ``None`` means "not approved yet": a
  ``REQUIRE_APPROVAL`` tool stops at ``approval_required`` and its handler is
  never called. Any other value must be a live
  :class:`~app.modules.approvals.grants.ApprovalGrant`, which only the
  approvals service mints (``execute_approved_action``, after claiming an
  approved, unexpired, payload-bound request). The executor spends it with
  ``consume_grant`` against the key it recomputes for *this* call; a string,
  an unminted or replayed grant, or a grant for another tool/input yields
  ``error``/``forbidden`` and the handler is never called. The HTTP layer
  never accepts a grant from clients.
- ``approval_request`` payload (``ApprovalRequestPayload``): ``tool_name``,
  the validated input as a plain dict, ``required_role`` — the approver role
  of *this* call from :func:`resolve_approval_role` (the tool's
  ``required_role`` unless its ``approval_role`` resolver asks for a more
  privileged one) as its string value — and ``action_key`` —
  ``"<tool_name>:<sha256(tool_name + canonical input JSON)>"`` — the stable
  identifier an approval is bound to. A resolver that fails is a
  ``tool_failed`` error: no payload is produced with a guessed role.

Audit (T8)
----------
Every call writes exactly one audit event with ``persist_event`` (its own
transaction: it survives the caller's rollback, and a handler's external
effects are recorded even if the caller's database work is undone):

========================== ============================== ===============================
outcome                     action                         payload / result
========================== ============================== ===============================
unknown tool                ``tool.not_found``             input field names / error
insufficient role           ``tool.denied``                input field names / error
inactive module             ``tool.inactive_module``       input field names / error + module
invalid input               ``tool.invalid_input``         input field names / error locs+types
precheck refused            ``tool.precheck_failed``       canonical input / error + reason
no grant (approval needed)  ``tool.approval_required``     canonical input / key + approver
rejected grant              ``tool.denied``                canonical input / error
dry run                     ``tool.dry_run``               canonical input / approval needed
handler/hook/output failure ``tool.failed``                canonical input / error
success                     ``tool.executed``              canonical input / output
========================== ============================== ===============================

Unvalidated input values are never stored, nor is the grant. The actor is
``user`` (``actor_type`` lets the T9 agent loop record ``agent``), and
``correlation_id`` threads related events (the approvals service passes the
approval id).
"""

import hashlib
import json
import logging
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.agent.llm import LLMProvider
from app.modules.approvals.grants import ApprovalGrant, consume_grant
from app.modules.audit.models import ActorType
from app.modules.audit.service import persist_event
from app.modules.documents.storage import DocumentStorage
from app.modules.identity.models import UserRole
from app.modules.retrieval.embeddings import EmbeddingProvider
from app.modules.tools.exceptions import (
    ToolApprovalRoleError,
    ToolNotFoundError,
    ToolPrecheckError,
)
from app.modules.tools.registry import (
    ApprovalPolicy,
    ToolContext,
    ToolRegistry,
    ToolSpec,
    ToolUser,
)

logger = logging.getLogger(__name__)

ErrorKind = Literal[
    "not_found",
    "forbidden",
    "inactive_module",
    "validation",
    "precheck",
    "execution",
]

#: Caller-facing message of a handler exception (the detail is audited only).
TOOL_FAILED = "tool_failed"
#: Caller-facing message of a handler return that violates ``output_model``.
INVALID_OUTPUT = "invalid tool output"
#: Code of a precheck refusal; the caller-facing message is ``"<code>: <reason>"``.
PRECHECK_FAILED = "precheck_failed"

#: Role precedence: lower number = more privileged. Mirrors UserRole's
#: declaration order ("ordered from most to least privileged").
_ROLE_RANK: dict[UserRole, int] = {
    UserRole.OWNER: 0,
    UserRole.ADMIN: 1,
    UserRole.OPERATOR: 2,
    UserRole.VIEWER: 3,
}


class ApprovalRequestPayload(BaseModel):
    """What must be approved before a ``REQUIRE_APPROVAL`` tool may run.

    The approvals service persists the equivalent ``ApprovalRequest`` row,
    recomputing ``action_key`` itself rather than trusting this value.
    """

    tool_name: str
    input: dict[str, Any]
    required_role: str
    action_key: str


class DryRunInfo(BaseModel):
    """What WOULD happen if the tool ran — produced without calling it."""

    tool_name: str
    kind: str
    approval_policy: str
    validated_input: dict[str, Any]
    #: For a dry run: whether actual execution would need human approval.
    approval_required: bool


class ToolExecutionResult(BaseModel):
    """Uniform outcome of one ``execute_tool`` call.

    ``error_kind`` is internal (an API layer uses it to choose 400 vs 403)
    and should not be exposed in a public response contract — clients branch
    on the HTTP status instead. ``approval_request`` is set only when
    ``status == "approval_required"``.
    """

    tool_name: str
    status: Literal["ok", "error", "approval_required", "dry_run"]
    output: Any | None = None
    error: str | None = None
    approval_request: ApprovalRequestPayload | None = None
    error_kind: ErrorKind | None = None


class _AuditRecord(NamedTuple):
    """What one ``execute_tool`` call writes to the audit trail."""

    action: str
    payload: dict[str, Any]
    result: dict[str, Any]


def _tool_failed(
    tool_name: str, canonical: dict[str, Any], exc: Exception
) -> tuple[ToolExecutionResult, _AuditRecord]:
    """Masked failure: callers get :data:`TOOL_FAILED`, the detail is audited only."""
    return ToolExecutionResult(
        tool_name=tool_name,
        status="error",
        error=TOOL_FAILED,
        error_kind="execution",
    ), _AuditRecord("tool.failed", canonical, {"error": str(exc) or type(exc).__name__})


def _input_fields(raw_input: Any) -> dict[str, Any]:
    """Field names of unvalidated input: never its values."""
    names = sorted(map(str, raw_input)) if isinstance(raw_input, dict) else []
    return {"input_fields": names}


def _error_summary(exc: ValidationError) -> list[dict[str, Any]]:
    """Location and type of each validation error, without inputs or messages."""
    return [
        {"loc": list(error["loc"]), "type": error["type"]}
        for error in exc.errors(include_url=False, include_context=False, include_input=False)
    ]


def _validation_message(exc: ValidationError) -> str:
    """``loc: message`` per error, without the offending input values."""
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    parts = [
        f"{'.'.join(str(part) for part in error['loc']) or 'input'}: {error['msg']}"
        for error in errors
    ]
    return "invalid input: " + "; ".join(parts)


def compute_action_key(tool_name: str, validated_input: BaseModel) -> str:
    """Stable identifier binding a tool name to its canonical input.

    ``sha256`` over ``tool_name`` plus the input's canonical JSON
    (sorted keys, no whitespace, JSON-mode values) prefixed with the tool
    name for readability. Same tool + same input ⇒ same key, on any process.
    """
    canonical = json.dumps(
        validated_input.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(f"{tool_name}\n{canonical}".encode()).hexdigest()
    return f"{tool_name}:{digest}"


def role_satisfies(user: ToolUser, required_role: UserRole) -> bool:
    """True when ``user``'s rank satisfies ``required_role`` (or superadmin)."""
    if user.is_superadmin:
        return True
    return _ROLE_RANK[user.role] <= _ROLE_RANK[required_role]


def resolve_approval_role(spec: ToolSpec, validated_input: BaseModel) -> UserRole:
    """Role that must approve this call of ``spec`` with ``validated_input``.

    The more privileged of ``spec.approval_role(validated_input)`` and
    ``spec.required_role``: an approver is never less privileged than who may
    invoke the tool. Without a resolver this is ``spec.required_role``.

    Raises:
        ToolApprovalRoleError: the resolver raised or returned a value that
            is not a :class:`UserRole` (callers never guess a role instead).
    """
    if spec.approval_role is None:
        return spec.required_role
    try:
        resolved = UserRole(spec.approval_role(validated_input))
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        raise ToolApprovalRoleError(f"approval role resolution failed: {detail}") from exc
    return min(resolved, spec.required_role, key=_ROLE_RANK.__getitem__)


async def execute_tool(
    registry: ToolRegistry,
    session: AsyncSession,
    user: ToolUser,
    tool_name: str,
    raw_input: dict[str, Any],
    dry_run: bool = False,
    approval_grant: ApprovalGrant | None = None,
    *,
    actor_type: ActorType = ActorType.USER,
    correlation_id: str | None = None,
    embeddings: EmbeddingProvider | None = None,
    llm: LLMProvider | None = None,
    storage: DocumentStorage | None = None,
    active_modules: set[str] | None = None,
    credentials_key: str | None = None,
    settings: Settings | None = None,
) -> ToolExecutionResult:
    """Execute ``tool_name`` under authorization, validation, and policy gates.

    ``active_modules`` is the tenant's activated catalog module keys (the
    agent loop and the approvals execution read them once per run with
    :func:`app.modules.catalog.service.active_module_keys`): a tool whose
    ``spec.module`` is not in the set is refused as ``inactive_module``,
    while core tools (``spec.module is None``) always run. ``None`` — the
    default — disables the gate entirely, preserving pre-module behavior
    for callers that do not gate.

    ``credentials_key`` is the Fernet key for per-user provider connections
    and ``settings`` the app configuration; both flow into the
    :class:`ToolContext` for tools whose precheck/handler must decrypt
    stored credentials or refresh tokens (calendar-mvp T3/D3). The chat
    router wires them from ``Settings``; other callers may leave them
    ``None`` and the tools fail closed with a precheck error.

    Check order (each failure returns immediately, handler is only reached by
    the last step):

    1. Lookup — unknown name ⇒ ``error``/``not_found``.
    2. Authorization — insufficient role ⇒ ``error``/``forbidden`` with a
       generic message that reveals neither the requirement nor internals.
    3. Module gate (only when ``active_modules`` is a set) — a tool whose
       ``spec.module`` is not in it ⇒ ``error``/``inactive_module`` before
       validation, approval, and the handler; core tools (``module=None``)
       never gate, and ``active_modules=None`` skips the gate entirely
       (pre-T2 callers keep their behavior).
    4. Validation — ``spec.input_model`` rejects ``raw_input`` ⇒
       ``error``/``validation`` listing each failing field (no input
       values); handler not called.
    5. Precheck (when the tool has one), with the same context the handler
       gets — :class:`ToolPrecheckError` ⇒ ``error``/``precheck`` with
       ``precheck_failed: <reason>``; any other exception ⇒
       ``error``/``execution`` with :data:`TOOL_FAILED`. It runs on every
       path below, so an approved call is re-checked right before its
       handler and a refused call never reaches the approval gate.
    6. Approval gate (``REQUIRE_APPROVAL`` and not ``dry_run``) — no
       ``approval_grant`` ⇒ ``approval_required`` carrying the payload to
       approve with the role from :func:`resolve_approval_role` (a failing
       resolver ⇒ ``error``/``execution``); a grant that ``consume_grant``
       rejects for this call's action key ⇒ ``error``/``forbidden``.
    7. Dry run ⇒ ``dry_run`` describing the call; handler not called.
    8. Handler run with a :class:`ToolContext` (``session``, the user's
       tenant, ``embeddings``, ``llm``, ``storage``, ``correlation_id``,
       ``credentials_key``, ``settings``) — exceptions ⇒ ``error``/``execution``
       with the generic :data:`TOOL_FAILED`; the exception text goes to the
       server log and the audit event only.
    9. Output validation — ``spec.output_model`` rejects the return ⇒
       ``error``/``execution`` with :data:`INVALID_OUTPUT`.

    Handlers own their own transaction: the executor neither commits nor
    rolls back ``session`` (the caller/router does, mirroring the documents
    router). Every outcome is then audited once (see the module docstring).
    """
    context = ToolContext(
        session=session,
        tenant_id=user.tenant_id,
        user=user,
        embeddings=embeddings,
        correlation_id=correlation_id,
        llm=llm,
        storage=storage,
        credentials_key=credentials_key,
        settings=settings,
    )
    result, record = await _evaluate(
        registry, context, tool_name, raw_input, dry_run, approval_grant, active_modules
    )
    await persist_event(
        session,
        tenant_id=user.tenant_id,
        action=record.action,
        actor_user_id=user.id,
        actor_type=actor_type,
        tool_name=result.tool_name,
        payload=record.payload,
        result=record.result,
        correlation_id=correlation_id,
    )
    return result


async def _evaluate(
    registry: ToolRegistry,
    context: ToolContext,
    tool_name: str,
    raw_input: dict[str, Any],
    dry_run: bool,
    approval_grant: ApprovalGrant | None,
    active_modules: set[str] | None,
) -> tuple[ToolExecutionResult, _AuditRecord]:
    """Run the gates of :func:`execute_tool`; return its result and audit record."""
    user = context.user
    try:
        spec = registry.get(tool_name)
    except ToolNotFoundError:
        return ToolExecutionResult(
            tool_name=tool_name,
            status="error",
            error=f"unknown tool: {tool_name}",
            error_kind="not_found",
        ), _AuditRecord("tool.not_found", _input_fields(raw_input), {"error": "unknown tool"})

    if not role_satisfies(user, spec.required_role):
        return ToolExecutionResult(
            tool_name=spec.name,
            status="error",
            error="forbidden",
            error_kind="forbidden",
        ), _AuditRecord("tool.denied", _input_fields(raw_input), {"error": "forbidden"})

    if active_modules is not None and spec.module is not None and spec.module not in active_modules:
        return ToolExecutionResult(
            tool_name=spec.name,
            status="error",
            error="inactive_module",
            error_kind="inactive_module",
        ), _AuditRecord(
            "tool.inactive_module",
            _input_fields(raw_input),
            {"error": "inactive_module", "module": spec.module},
        )

    # ToolRegistry.register guarantees input_model is a BaseModel subclass.
    input_model = spec.input_model
    assert input_model is not None  # registration invariant
    try:
        validated_input = input_model.model_validate(raw_input)
    except ValidationError as exc:
        return ToolExecutionResult(
            tool_name=spec.name,
            status="error",
            error=_validation_message(exc),
            error_kind="validation",
        ), _AuditRecord(
            "tool.invalid_input", _input_fields(raw_input), {"errors": _error_summary(exc)}
        )

    canonical = validated_input.model_dump(mode="json")
    if spec.precheck is not None:
        try:
            await spec.precheck(context, validated_input)
        except ToolPrecheckError as exc:
            return ToolExecutionResult(
                tool_name=spec.name,
                status="error",
                error=f"{PRECHECK_FAILED}: {exc.reason}",
                error_kind="precheck",
            ), _AuditRecord(
                "tool.precheck_failed",
                canonical,
                {"error": PRECHECK_FAILED, "reason": exc.reason},
            )
        except Exception as exc:
            logger.exception("tool %s precheck failed", spec.name)
            return _tool_failed(spec.name, canonical, exc)

    if spec.approval_policy is ApprovalPolicy.REQUIRE_APPROVAL and not dry_run:
        action_key = compute_action_key(spec.name, validated_input)
        if approval_grant is None:
            try:
                approver = resolve_approval_role(spec, validated_input)
            except ToolApprovalRoleError as exc:
                logger.exception("tool %s could not resolve its approver role", spec.name)
                return _tool_failed(spec.name, canonical, exc)
            payload = ApprovalRequestPayload(
                tool_name=spec.name,
                input=canonical,
                required_role=approver.value,
                action_key=action_key,
            )
            return ToolExecutionResult(
                tool_name=spec.name,
                status="approval_required",
                approval_request=payload,
            ), _AuditRecord(
                "tool.approval_required",
                canonical,
                {"action_key": action_key, "required_role": approver.value},
            )
        if not consume_grant(approval_grant, action_key):
            logger.warning("tool %s refused an invalid approval grant", spec.name)
            return ToolExecutionResult(
                tool_name=spec.name,
                status="error",
                error="approval grant rejected",
                error_kind="forbidden",
            ), _AuditRecord("tool.denied", canonical, {"error": "approval grant rejected"})

    if dry_run:
        needs_approval = spec.approval_policy is ApprovalPolicy.REQUIRE_APPROVAL
        info = DryRunInfo(
            tool_name=spec.name,
            kind=spec.kind.value,
            approval_policy=spec.approval_policy.value,
            validated_input=canonical,
            approval_required=needs_approval,
        )
        return ToolExecutionResult(
            tool_name=spec.name, status="dry_run", output=info
        ), _AuditRecord("tool.dry_run", canonical, {"approval_required": needs_approval})

    try:
        output = await spec.handler(context, validated_input)
    except Exception as exc:
        logger.exception("tool %s failed", spec.name)
        return _tool_failed(spec.name, canonical, exc)

    if spec.output_model is not None:
        try:
            output = spec.output_model.model_validate(output)
        except ValidationError as exc:
            logger.error(
                "tool %s returned output violating %s",
                spec.name,
                spec.output_model.__name__,
            )
            return ToolExecutionResult(
                tool_name=spec.name,
                status="error",
                error=INVALID_OUTPUT,
                error_kind="execution",
            ), _AuditRecord(
                "tool.failed",
                canonical,
                {"error": INVALID_OUTPUT, "errors": _error_summary(exc)},
            )

    return ToolExecutionResult(tool_name=spec.name, status="ok", output=output), _AuditRecord(
        "tool.executed", canonical, {"output": output}
    )
