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
============================ ==================== ==================

Role checks compare an explicit rank map (lower = more privileged), mirroring
the declaration order of :class:`~app.modules.identity.models.UserRole`:
``owner=0, admin=1, operator=2, viewer=3``. A user may invoke a tool when
``rank(user.role) <= rank(spec.required_role)`` or ``user.is_superadmin``
(the superadmin bypass mirrors :func:`app.modules.identity.deps.require_roles`).

Approval seam for T7 (approvals module)
---------------------------------------
- ``approval_token: str | None`` — ``None`` means "not approved yet": a
  ``REQUIRE_APPROVAL`` tool stops at ``approval_required`` and its handler is
  never called. T7 is the only caller allowed to pass a token, and T7 must
  validate it against a persisted, unexpired, payload-bound ApprovalRequest
  before passing it here. The HTTP layer never accepts a token from clients.
- ``approval_request`` payload (``ApprovalRequestPayload``): ``tool_name``,
  the validated input as a plain dict, ``required_role`` (its string value),
  and ``action_key`` — ``"<tool_name>:<sha256(tool_name + canonical input
  JSON)>"`` — the stable identifier T7 binds an approval to (decision A7:
  exact action + payload binding).
"""

import hashlib
import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import UserRole
from app.modules.tools.exceptions import ToolNotFoundError
from app.modules.tools.registry import ApprovalPolicy, ToolRegistry, ToolUser

logger = logging.getLogger(__name__)

ErrorKind = Literal["not_found", "forbidden", "validation", "execution"]

#: Role precedence: lower number = more privileged. Mirrors UserRole's
#: declaration order ("ordered from most to least privileged").
_ROLE_RANK: dict[UserRole, int] = {
    UserRole.OWNER: 0,
    UserRole.ADMIN: 1,
    UserRole.OPERATOR: 2,
    UserRole.VIEWER: 3,
}


class ApprovalRequestPayload(BaseModel):
    """Seam T7 binds to: what must be approved before a tool may run.

    T7 replaces/extends this with its persisted ApprovalRequest rows; the
    field names here are the contract ``execute_tool`` produces today.
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


def _authorized(user: ToolUser, required_role: UserRole) -> bool:
    """True when ``user``'s rank satisfies ``required_role`` (or superadmin)."""
    if user.is_superadmin:
        return True
    return _ROLE_RANK[user.role] <= _ROLE_RANK[required_role]


async def execute_tool(
    registry: ToolRegistry,
    session: AsyncSession,
    user: ToolUser,
    tool_name: str,
    raw_input: dict[str, Any],
    dry_run: bool = False,
    approval_token: str | None = None,
) -> ToolExecutionResult:
    """Execute ``tool_name`` under authorization, validation, and policy gates.

    Check order (each failure returns immediately, handler is only reached by
    the last step):

    1. Lookup — unknown name ⇒ ``error``/``not_found``.
    2. Authorization — insufficient role ⇒ ``error``/``forbidden`` with a
       generic message that reveals neither the requirement nor internals.
    3. Validation — ``spec.input_model`` rejects ``raw_input`` ⇒
       ``error``/``validation`` with Pydantic's message; handler not called.
    4. Approval gate — ``REQUIRE_APPROVAL`` without ``approval_token`` and
       not ``dry_run`` ⇒ ``approval_required`` carrying the T7 payload.
    5. Dry run ⇒ ``dry_run`` describing the call; handler not called.
    6. Handler run — exceptions ⇒ ``error``/``execution`` (message only; the
       traceback goes to the server log, never to the API).
    7. Output validation — ``spec.output_model`` rejects the return ⇒
       ``error``/``execution``.

    Handlers own their own transaction: the executor neither commits nor
    rolls back ``session`` (the caller/router does, mirroring the documents
    router).
    """
    try:
        spec = registry.get(tool_name)
    except ToolNotFoundError:
        return ToolExecutionResult(
            tool_name=tool_name,
            status="error",
            error=f"unknown tool: {tool_name}",
            error_kind="not_found",
        )

    if not _authorized(user, spec.required_role):
        return ToolExecutionResult(
            tool_name=spec.name,
            status="error",
            error="forbidden",
            error_kind="forbidden",
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
            error=str(exc),
            error_kind="validation",
        )

    if (
        spec.approval_policy is ApprovalPolicy.REQUIRE_APPROVAL
        and approval_token is None
        and not dry_run
    ):
        payload = ApprovalRequestPayload(
            tool_name=spec.name,
            input=validated_input.model_dump(mode="json"),
            required_role=spec.required_role.value,
            action_key=compute_action_key(spec.name, validated_input),
        )
        return ToolExecutionResult(
            tool_name=spec.name,
            status="approval_required",
            approval_request=payload,
        )

    if dry_run:
        info = DryRunInfo(
            tool_name=spec.name,
            kind=spec.kind.value,
            approval_policy=spec.approval_policy.value,
            validated_input=validated_input.model_dump(mode="json"),
            approval_required=spec.approval_policy is ApprovalPolicy.REQUIRE_APPROVAL,
        )
        return ToolExecutionResult(tool_name=spec.name, status="dry_run", output=info)

    try:
        output = await spec.handler(session, user.tenant_id, user, validated_input)
    except Exception as exc:
        logger.exception("tool %s failed", spec.name)
        return ToolExecutionResult(
            tool_name=spec.name,
            status="error",
            error=str(exc) or type(exc).__name__,
            error_kind="execution",
        )

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
                error=f"invalid tool output: {exc}",
                error_kind="execution",
            )

    return ToolExecutionResult(tool_name=spec.name, status="ok", output=output)
