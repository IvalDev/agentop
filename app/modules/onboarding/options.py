"""In-process registry of per-module dynamic ``scope_select`` options.

A ``scope_select`` step's engine default is *static*: the synthesized
``scopes`` field offers the module's declared ``permissions`` and the engine
validates answers against that allowlist. A module whose scope choice lives
in the provider's data (not in the OAuth permission list) registers a
*scope options provider* here instead — the same explicit-registry style as
:func:`register_tester <app.modules.onboarding.testers.register_tester>`:
importing this module registers nothing; the module package wires its
provider through an explicit entry point, and tests prefer registering on
this shared dict (cleared by their fixtures) over process leftovers.

Contract of a provider:

- it receives a :class:`ScopeOptionsContext` (the caller's principal, the
  request session, the module key, the credentials key, and the settings)
  and answers :class:`ScopeOptions` — the option values to render plus the
  ``exclusive`` flag when the step accepts exactly one choice;
- it raises :class:`ScopeOptionsError` when the options cannot be produced
  (no stored connection, dead tokens, provider outage). The engine maps
  that — and any unexpected raise — to a fail-closed error state: the
  rendered step carries an empty option list plus one fixed actionable
  message, and a submit is refused. A provider must never log or persist
  the decrypted credentials it can reach through the session.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.identity.deps import CurrentUser


@dataclass(frozen=True)
class ScopeOptionsContext:
    """What a scope options provider may see for one render or submit.

    ``caller`` is the authenticated principal of the caller's own flow;
    ``session`` is the request's database session (a provider may READ the
    caller's stored connection through it); ``credentials_key``/``settings``
    let a provider decrypt and refresh that connection the same way the
    calendar precheck does. Secrets seen through this context stay in
    memory for the current call only.
    """

    caller: CurrentUser
    session: AsyncSession
    module_key: str
    credentials_key: str | None
    settings: Settings | None


@dataclass(frozen=True)
class ScopeOptions:
    """Dynamic options of one ``scope_select`` step.

    ``options`` are the choice values the renderer offers and the engine
    validates answers against (replacing ``manifest.permissions``).
    ``exclusive`` enforces exactly one choice — the google-mail MVP is
    whole-mailbox OR one label.
    """

    options: tuple[str, ...]
    exclusive: bool = False


class ScopeOptionsError(Exception):
    """The module could not produce the options (engine: fail closed)."""


#: Async callable: options for the caller's current scope_select step.
ScopeOptionsProvider = Callable[[ScopeOptionsContext], Awaitable[ScopeOptions]]

#: Process-wide registry, empty in production; keyed by ``ModuleManifest.key``.
scope_options_providers: dict[str, ScopeOptionsProvider] = {}


def register_scope_options_provider(module_key: str, provider: ScopeOptionsProvider) -> None:
    """Register ``provider`` for ``module_key`` on the process-wide registry.

    Explicit registration (the Python module cache keeps the calling entry
    point idempotent across ``create_app`` calls in tests), overwriting like
    :func:`register_tester <app.modules.onboarding.testers.register_tester>`.
    """
    scope_options_providers[module_key] = provider


__all__ = [
    "ScopeOptions",
    "ScopeOptionsContext",
    "ScopeOptionsError",
    "ScopeOptionsProvider",
    "register_scope_options_provider",
    "scope_options_providers",
]
