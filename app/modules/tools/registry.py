"""Typed tool specifications and the process-wide tool registry.

A *tool* is a named, typed capability the agent may invoke on behalf of an
authenticated user. Every tool declares:

- its :class:`ToolKind` — ``EXTRACT`` (read-only, produces structured data)
  or ``ACTION`` (mutates external state or sends something);
- its :class:`ApprovalPolicy` — ``AUTO`` may run without human approval and
  is only legal for ``EXTRACT``; ``ALLOW_WITHOUT_APPROVAL`` also runs
  without approval and is the explicit no-approval policy for deliberate
  self-service ``ACTION`` tools (calendar-mvp D5: the user's own calendar —
  the model acts on the caller's behalf with no third-party effect, so a
  human approval round-trip would only add friction; the registration
  invariant still forbids ``AUTO`` on ``ACTION``, so an action must declare
  its no-approval policy explicitly); ``EXTRACT`` tools may use it as the
  clearer spelling of ``AUTO``; ``REQUIRE_APPROVAL`` must be approved by a
  human first (feature decision A7: the model never authorizes);
- the minimum :class:`~app.modules.identity.models.role` that may invoke it;
- the catalog ``module`` key it belongs to: ``None`` marks a core tool every
  tenant always has, while a non-``None`` key (a lowercase identifier like a
  tool name) is offered and executed only while the tenant has that module
  active (see :func:`app.modules.agent.service.tool_schemas` and
  :func:`app.modules.tools.executor.execute_tool`);
- a Pydantic ``input_model`` describing its typed input, and optionally an
  ``output_model`` the handler's return value is validated against.

Handler contract (exact signature)::

    async def handler(context: ToolContext, validated_input: BaseModel) -> Any | BaseModel

:class:`ToolContext` is built by the executor for every call: the caller's
``session``, the ``tenant_id`` and ``user`` of the authenticated principal,
and the optional services a handler may need (the ``embeddings`` provider for
semantic search, the chat model ``llm``, the document blob ``storage``, the
run's ``correlation_id``, the requesting user's ``timezone``). ``tenant_id``
is the isolation key (decision A8): handlers MUST scope every query with it.
``ToolUser`` is a structural protocol satisfied by both the ORM ``User`` and
the request-scoped ``CurrentUser`` value object, so handlers stay decoupled
from either. New services are added as context fields, never as extra
positional arguments.

Optional hooks (T11)::

    def approval_role(validated_input: BaseModel) -> UserRole
    async def precheck(context: ToolContext, validated_input: BaseModel) -> None

- ``approval_role`` (``REQUIRE_APPROVAL`` tools only) chooses who must approve
  *this* call, e.g. by invoice amount. It must be pure and deterministic: the
  approvals service stores its result as the request's ``required_role`` and
  re-derives it from the stored payload before execution. The effective
  approver is never less privileged than ``required_role`` (see
  :func:`~app.modules.tools.executor.resolve_approval_role`).
- ``precheck`` verifies the call against current tenant state (duplicates,
  references that must exist) before the approval gate, and again right
  before the handler of an approved call. It refuses by raising
  :class:`~app.modules.tools.exceptions.ToolPrecheckError` with a short,
  user-safe reason; it must not change state.

Requester-only approval (google-mail G7)::

    requester_only_approval: bool = False

- ``requester_only_approval`` (``REQUIRE_APPROVAL`` tools only) marks a tool
  that acts on the *requester's own* account — a per-user provider
  connection such as their Gmail mailbox. The handler always runs with the
  identity of whoever executes the approved call, so for such a tool another
  user of the tenant would act on *their* account instead. With the flag set
  only the request's ``requested_by`` may approve, reject, or execute it (and
  still needs the stored approver role); reads and listing are unchanged
  (see :mod:`app.modules.approvals.service`). Declarative on purpose: the
  approvals service reads it from the registry, never from the request.

Registration invariants are enforced by :meth:`ToolRegistry.register` and
raise :class:`ToolRegistrationError`; lookups raise
:class:`ToolNotFoundError`. Single-process MVP: no thread-safety guarantees.
"""

import enum
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.agent.llm import LLMProvider
from app.modules.connectors.hub import ConnectorRequester
from app.modules.documents.storage import DocumentStorage
from app.modules.identity.models import UserRole
from app.modules.retrieval.embeddings import EmbeddingProvider
from app.modules.tools.exceptions import ToolNotFoundError, ToolRegistrationError


class ToolKind(enum.StrEnum):
    """What a tool does: read-only extraction vs. state-changing action."""

    EXTRACT = "extract"
    ACTION = "action"


class ApprovalPolicy(enum.StrEnum):
    """Whether a human must approve the tool before it executes."""

    AUTO = "auto"
    ALLOW_WITHOUT_APPROVAL = "allow_without_approval"
    REQUIRE_APPROVAL = "require_approval"


class ToolUser(Protocol):
    """Authenticated principal a handler receives (``User`` or ``CurrentUser``)."""

    id: int
    tenant_id: int
    role: UserRole
    is_superadmin: bool


@dataclass(frozen=True)
class ToolContext:
    """Everything a handler may use besides its validated input.

    ``tenant_id`` always equals ``user.tenant_id`` (the executor builds it
    from the authenticated principal, never from tool input). ``embeddings``,
    ``llm``, and ``storage`` are ``None`` when the caller wired no such
    service; handlers must degrade or fail closed instead of assuming one.
    ``credentials_key`` is the Fernet key for decrypting per-user provider
    connections (calendar-mvp T3); ``settings`` carries the app configuration
    a handler's precheck may need (the Google OAuth client credentials for
    the under-demand token refresh, D3). Both default to ``None`` — tools
    that need them must fail closed with a precheck error, never a 500.
    ``timezone`` is the resolved IANA name of the requesting user in a chat
    run (``"UTC"`` when the browser sent none or an unknown one); it is
    ``None`` outside chat runs, e.g. when an approved request executes.
    ``connectors`` is the live hub of the user's local workspace devices;
    folder tools fail their precheck when it is ``None``.
    """

    session: AsyncSession
    tenant_id: int
    user: ToolUser
    embeddings: EmbeddingProvider | None = None
    correlation_id: str | None = None
    llm: LLMProvider | None = None
    storage: DocumentStorage | None = None
    credentials_key: str | None = None
    settings: Settings | None = None
    connectors: ConnectorRequester | None = None
    timezone: str | None = None


ToolHandler = Callable[[ToolContext, BaseModel], Awaitable[Any]]
#: Per-request approver choice: a pure function of the validated input.
ApprovalRoleResolver = Callable[[BaseModel], UserRole]
#: State verification run before the approval gate and before the handler.
ToolPrecheck = Callable[[ToolContext, BaseModel], Awaitable[None]]

# Lowercase identifier-ish names such as ``extract_invoice`` / ``send_email``.
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class ToolSpec:
    """Immutable registration record of a single tool.

    A frozen dataclass rather than a Pydantic model: this is an in-process
    registration record, not data crossing an I/O boundary, and the explicit
    checks in :meth:`ToolRegistry.register` must surface as
    ``ToolRegistrationError`` (not a wrapped ``ValidationError``).
    ``input_model`` may be ``None`` on a bare construction but is required
    at registration time. ``required_role`` is who may *invoke* the tool;
    ``module`` is which catalog module owns it — ``None`` marks a core tool
    that is never gated by tenant activation, a non-``None`` key must match
    the tool-name identifier pattern (checked at registration);
    ``approval_role`` and ``precheck`` are the optional hooks described in
    the module docstring, as is ``requester_only_approval`` (who may decide
    and execute an approval of this tool).
    """

    name: str
    description: str
    kind: ToolKind
    approval_policy: ApprovalPolicy
    required_role: UserRole
    handler: ToolHandler
    input_model: type[BaseModel] | None = None
    output_model: type[BaseModel] | None = None
    approval_role: ApprovalRoleResolver | None = None
    precheck: ToolPrecheck | None = None
    module: str | None = None
    requester_only_approval: bool = False


class ToolRegistry:
    """Name-keyed collection of validated tool specifications."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """Validate ``spec`` against every invariant, then store it.

        Raises:
            ToolRegistrationError: duplicate or invalid name, missing or
                non-Pydantic ``input_model``, non-callable handler or hook,
                an ``ACTION`` tool that is not ``REQUIRE_APPROVAL``, an
                ``approval_role`` or ``requester_only_approval`` on a tool
                that is not ``REQUIRE_APPROVAL``, or an invalid ``module`` key.
        """
        if not isinstance(spec.name, str) or not _NAME_PATTERN.fullmatch(spec.name):
            raise ToolRegistrationError(
                f"invalid tool name {spec.name!r}: expected lowercase identifier "
                "like 'extract_invoice'"
            )
        if spec.name in self._specs:
            raise ToolRegistrationError(f"duplicate tool name: {spec.name!r}")
        if spec.module is not None and (
            not isinstance(spec.module, str) or not _NAME_PATTERN.fullmatch(spec.module)
        ):
            raise ToolRegistrationError(
                f"tool {spec.name!r}: invalid module {spec.module!r}: expected lowercase "
                "identifier like 'calendar'"
            )
        if spec.input_model is None or not (
            isinstance(spec.input_model, type) and issubclass(spec.input_model, BaseModel)
        ):
            raise ToolRegistrationError(
                f"tool {spec.name!r} needs an input_model subclassing BaseModel"
            )
        if not callable(spec.handler):
            raise ToolRegistrationError(f"tool {spec.name!r} needs a callable handler")
        if spec.kind is ToolKind.ACTION and spec.approval_policy not in (
            ApprovalPolicy.REQUIRE_APPROVAL,
            ApprovalPolicy.ALLOW_WITHOUT_APPROVAL,
        ):
            raise ToolRegistrationError(
                f"tool {spec.name!r}: ACTION tools must declare approval_policy="
                "REQUIRE_APPROVAL or ALLOW_WITHOUT_APPROVAL (never AUTO)"
            )
        for hook in ("approval_role", "precheck"):
            if getattr(spec, hook) is not None and not callable(getattr(spec, hook)):
                raise ToolRegistrationError(f"tool {spec.name!r}: {hook} must be callable")
        if (
            spec.approval_role is not None
            and spec.approval_policy is not ApprovalPolicy.REQUIRE_APPROVAL
        ):
            raise ToolRegistrationError(
                f"tool {spec.name!r}: approval_role needs approval_policy=REQUIRE_APPROVAL"
            )
        if (
            spec.requester_only_approval
            and spec.approval_policy is not ApprovalPolicy.REQUIRE_APPROVAL
        ):
            raise ToolRegistrationError(
                f"tool {spec.name!r}: requester_only_approval needs "
                "approval_policy=REQUIRE_APPROVAL"
            )
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        """Return the spec registered under ``name``.

        Raises:
            ToolNotFoundError: no tool with that name.
        """
        try:
            return self._specs[name]
        except KeyError:
            raise ToolNotFoundError(f"unknown tool: {name!r}") from None

    def list_specs(self) -> list[ToolSpec]:
        """Return every registered spec in registration order."""
        return list(self._specs.values())

    def names(self) -> list[str]:
        """Return every registered tool name in registration order."""
        return list(self._specs)


#: Process-wide registry populated by ``app.modules.tools.builtin`` at import.
default_registry = ToolRegistry()


def register_default(spec: ToolSpec) -> ToolSpec:
    """Register ``spec`` on :data:`default_registry` and return it.

    Explicit import-time registration (Python's module cache makes it
    idempotent across ``create_app`` calls in tests).
    """
    default_registry.register(spec)
    return spec
