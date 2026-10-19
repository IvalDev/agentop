"""Registration entry points for the calendar module.

Everything this module *can* register lives behind explicit functions:
importing :mod:`app.modules.calendar` (or this module) has **no** side
effects, because the production import graph must keep ``default_catalog``
empty until a reviewed allowlist change lands (human decision on the T1
slice, 2026-10-15). Consequences until then: the production
``GET /api/v1/modules`` does not list ``calendar``, and no tenant can
activate it — intentional, pending that review.

Entry points:

- :func:`register_calendar` — register the manifest on *any* catalog; the
  tests use it with a local :class:`~app.modules.catalog.registry.ModuleCatalog`
  (the local-fixture pattern of ``tests/test_catalog_registry.py``);
- :func:`register_default_calendar` — the production wiring: ``app.main``
  executes it once at import time (reviewed calendar-mvp allowlist change);
  a second call raises ``ModuleRegistrationError``;
- :func:`register_google_provider` / :func:`register_calendar_tester` — wire
  the process-wide provider and tester registries the same explicit way;
  ``app.main`` calls them once at import, importing this module alone stays
  free of side effects.

Everything here is import-safe: module imports perform **no** registration.
"""

from app.modules.calendar.manifest import CALENDAR_MANIFEST
from app.modules.calendar.provider import GoogleCalendarProvider, register_provider
from app.modules.calendar.testers import calendar_connection_tester
from app.modules.catalog.manifest import ModuleManifest
from app.modules.catalog.registry import ModuleCatalog, register_default
from app.modules.onboarding.testers import register_tester


def register_calendar(catalog: ModuleCatalog) -> ModuleManifest:
    """Register the calendar manifest on ``catalog`` and return it.

    Raises:
        ModuleRegistrationError: the catalog already holds ``calendar`` or
            the manifest violates an invariant (same rules as any module).
    """
    catalog.register(CALENDAR_MANIFEST)
    return CALENDAR_MANIFEST


def register_default_calendar() -> ModuleManifest:
    """Register the calendar manifest on the process-wide default catalog.

    Executed exactly once by the production import graph (``app.main``, the
    human-approved calendar allowlist change): a second call raises
    ``ModuleRegistrationError``, which is the duplicate-registration contract.
    """
    return register_default(CALENDAR_MANIFEST)


def register_google_provider() -> None:
    """Register the Google adapter factory under provider name ``google``.

    Process-wide and explicit, like every entry point here: importing this
    module registers nothing; ``app.main`` calls it once at import time.
    Overwrites an existing registration — same contract as ``register_tester``.
    """
    register_provider("google", GoogleCalendarProvider)


def register_calendar_tester() -> None:
    """Register :func:`calendar_connection_tester` for the ``calendar`` module.

    Same explicit, import-free contract as :func:`register_google_provider`.
    """
    register_tester("calendar", calendar_connection_tester)


__all__ = [
    "register_calendar",
    "register_calendar_tester",
    "register_default_calendar",
    "register_google_provider",
]
