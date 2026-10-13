"""Typed tool specifications and the process-wide tool registry.

A *tool* is a named, typed capability the agent may invoke on behalf of an
authenticated user. Every tool declares:

- its :class:`ToolKind` — ``EXTRACT`` (read-only, produces structured data)
  or ``ACTION`` (mutates external state or sends something);
- its :class:`ApprovalPolicy` — ``AUTO`` may run without human approval and
  is only legal for ``EXTRACT``; ``REQUIRE_APPROVAL`` must be approved by a
  human first and is mandatory for every ``ACTION`` (feature decision A7:
  the model never authorizes);
- the minimum :class:`~app.modules.identity.models.role` that may invoke it;
- a Pydantic ``input_model`` describing its typed input, and optionally an
  ``output_model`` the handler's return value is validated against.

Handler contract (exact signature)::

    async def handler(
        session: AsyncSession,
        tenant_id: int,
        user: ToolUser,
        validated_input: BaseModel,
    ) -> Any | BaseModel

``tenant_id`` comes from the authenticated principal and is the isolation key
(decision A8): handlers MUST scope every query with it. ``ToolUser`` is a
structural protocol satisfied by both the ORM ``User`` and the request-scoped
``CurrentUser`` value object, so handlers stay decoupled from either.

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

from app.modules.identity.models import UserRole
from app.modules.tools.exceptions import ToolNotFoundError, ToolRegistrationError


class ToolKind(enum.StrEnum):
    """What a tool does: read-only extraction vs. state-changing action."""

    EXTRACT = "extract"
    ACTION = "action"


class ApprovalPolicy(enum.StrEnum):
    """Whether a human must approve the tool before it executes."""

    AUTO = "auto"
    REQUIRE_APPROVAL = "require_approval"


class ToolUser(Protocol):
    """Authenticated principal a handler receives (``User`` or ``CurrentUser``)."""

    id: int
    tenant_id: int
    role: UserRole
    is_superadmin: bool


ToolHandler = Callable[[AsyncSession, int, ToolUser, BaseModel], Awaitable[Any]]

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
    at registration time.
    """

    name: str
    description: str
    kind: ToolKind
    approval_policy: ApprovalPolicy
    required_role: UserRole
    handler: ToolHandler
    input_model: type[BaseModel] | None = None
    output_model: type[BaseModel] | None = None


class ToolRegistry:
    """Name-keyed collection of validated tool specifications."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """Validate ``spec`` against every invariant, then store it.

        Raises:
            ToolRegistrationError: duplicate or invalid name, missing or
                non-Pydantic ``input_model``, non-callable handler, or an
                ``ACTION`` tool that is not ``REQUIRE_APPROVAL``.
        """
        if not isinstance(spec.name, str) or not _NAME_PATTERN.fullmatch(spec.name):
            raise ToolRegistrationError(
                f"invalid tool name {spec.name!r}: expected lowercase identifier "
                "like 'extract_invoice'"
            )
        if spec.name in self._specs:
            raise ToolRegistrationError(f"duplicate tool name: {spec.name!r}")
        if spec.input_model is None or not (
            isinstance(spec.input_model, type) and issubclass(spec.input_model, BaseModel)
        ):
            raise ToolRegistrationError(
                f"tool {spec.name!r} needs an input_model subclassing BaseModel"
            )
        if not callable(spec.handler):
            raise ToolRegistrationError(f"tool {spec.name!r} needs a callable handler")
        if (
            spec.kind is ToolKind.ACTION
            and spec.approval_policy is not ApprovalPolicy.REQUIRE_APPROVAL
        ):
            raise ToolRegistrationError(
                f"tool {spec.name!r}: ACTION tools must use approval_policy=REQUIRE_APPROVAL"
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
