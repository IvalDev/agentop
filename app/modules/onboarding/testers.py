"""In-process registry of per-module connection testers.

A ``test_connection`` onboarding step asks the module's registered tester
whether the accumulated answers actually reach the provider. The registry is
a plain module-level dict keyed by ``module_key`` — same single-process MVP
contract as the tool registry and the default module catalog.

Production ships :data:`connection_testers` **empty**: this foundations
slice registers no real module, so every ``test_connection`` step fails
closed (``TesterNotRegisteredError``) until a module package registers its
tester at import time with :func:`register_tester`. Test apps never touch
the global dict: they swap ``app.state.connection_testers`` for a fresh
local one, exactly like ``app.state.tool_registry``.

A tester receives a :class:`ConnectionContext` — the decrypted secret
answers plus the non-secret ones, in memory for this call only — and
returns ``True`` when the connection works, ``False`` when it does not, or
raises when the check itself could not run. A tester must never log or
persist the context's secrets.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConnectionContext:
    """What a connection tester is allowed to see for one verification.

    ``credentials`` holds the accumulated secret answers (decrypted, in
    memory for this call only); ``data`` holds the non-secret answers. The
    context never travels back into the database or an audit payload.
    """

    tenant_id: int
    user_id: int
    module_key: str
    provider: str
    credentials: dict[str, str]
    data: dict[str, Any]


#: Async callable: ``True`` = connection works, ``False`` = it does not.
ConnectionTester = Callable[[ConnectionContext], Awaitable[bool]]

#: Process-wide registry, empty in production; keyed by ``ModuleManifest.key``.
connection_testers: dict[str, ConnectionTester] = {}


def register_tester(module_key: str, tester: ConnectionTester) -> None:
    """Register ``tester`` for ``module_key`` on the process-wide registry.

    Explicit import-time registration (the Python module cache makes it
    idempotent across ``create_app`` calls in tests). Tests should prefer a
    local dict on ``app.state.connection_testers`` over this global.
    """
    connection_testers[module_key] = tester
