"""Declarative module manifests: what a module is and how it onboards.

A *module* is a named bundle of agent capability (tools plus the permission
scopes they need) that a tenant's admin switches on from the settings panel.
The manifest is pure data registered in code at import time
(:mod:`app.modules.catalog.registry`); it declares the module's identity
(``key``, ``name``, ``description``, ``version``), the scopes its tools would
request (``permissions``), the tool names it contributes (``tools``), and the
onboarding flow each user walks to connect their own account (``onboarding``).

Onboarding steps are declared, not executed: the T4 engine will interpret
them per user x module. A step's ``type`` is one of the four shapes that
engine supports — ``form`` (fields the user fills in), ``redirect`` (send the
user to the provider's consent page), ``scope_select`` (pick which granted
scopes to allow), and ``test_connection`` (verify the stored credentials).
``FieldSpec.secret`` marks fields whose values must be encrypted toward the
connection and never persisted in clear text — the manifest only declares
the flag; the crypto arrives with T3/T4, never here.

Manifests are frozen dataclasses: registration-time state cannot drift, and
invariants are enforced once by :meth:`~app.modules.catalog.registry.ModuleCatalog.register`
(a dataclass performs no validation of its own, hence the explicit checks
there — the same split as ``ToolSpec``).
"""

from dataclasses import dataclass
from typing import Literal, get_args

#: Onboarding step shapes the T4 engine will interpret.
StepType = Literal["form", "redirect", "scope_select", "test_connection"]

#: Widget types a declarative ``form`` step may declare for its fields.
FieldType = Literal["text", "email", "password", "url", "number", "select"]

#: Runtime membership test for :data:`StepType` (a Literal is not enforced at runtime).
STEP_TYPES: frozenset[str] = frozenset(get_args(StepType))


@dataclass(frozen=True)
class FieldSpec:
    """One input of a ``form`` step, declared for the future onboarding renderer.

    ``secret`` is the contract T4 enforces: such a field's value is encrypted
    toward the user's connection and must never appear in onboarding state,
    API responses, logs, audit payloads, or model messages. Display metadata
    (``label``, ``help_text``) keeps the renderer code free of per-module
    branches.
    """

    key: str
    label: str
    type: FieldType = "text"
    required: bool = True
    secret: bool = False
    help_text: str = ""


@dataclass(frozen=True)
class StepSpec:
    """One onboarding step; ``key`` addresses it inside the flow (resume point).

    Display metadata (``title``, ``description``) is declarative so the T4/T5
    renderer needs no per-module code. ``fields`` only applies to ``form``
    steps; the other step types carry no inputs today. This record is data
    only — no step runs before the T4 engine exists.
    """

    key: str
    type: StepType
    title: str = ""
    description: str = ""
    fields: tuple[FieldSpec, ...] = ()


@dataclass(frozen=True)
class ModuleManifest:
    """Immutable registration record of one module (see the module docstring).

    Built by hand at registration sites rather than parsed by Pydantic: like
    ``ToolSpec``, this is an in-process declaration, and invariant failures
    must surface as ``ModuleRegistrationError`` (not a wrapped
    ``ValidationError``). ``permissions`` are the OAuth/API scopes the module's
    tools would request — never credentials; ``tools`` are the tool names the
    module contributes, which T2 gates on the tenant's active modules.
    """

    key: str
    name: str
    description: str
    version: str
    permissions: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    onboarding: tuple[StepSpec, ...] = ()
