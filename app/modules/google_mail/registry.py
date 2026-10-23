"""Registration entry points for the google_mail module.

Everything this module *can* register lives behind explicit functions:
importing :mod:`app.modules.google_mail` (or this module) has **no** side
effects, because the production import graph keeps ``default_catalog``
populated only by reviewed allowlist changes (the google-mail G1 change,
2026-10-18, joins the earlier calendar one).

Entry points:

- :func:`register_google_mail` — register the manifest on *any* catalog; the
  tests use it with a local :class:`~app.modules.catalog.registry.ModuleCatalog`
  (the local-fixture pattern of ``tests/test_catalog_registry.py``);
- :func:`register_default_google_mail` — the production wiring: ``app.main``
  executes it once at import time (reviewed google-mail allowlist change);
  a second call raises ``ModuleRegistrationError``. It ALSO registers the
  module's scope options provider (G2) AND the seven chat tools on the
  process-wide tool registry (G3) — both folds live here because the
  reviewed ``app.main`` block calls this function once and its own comment
  points here (the manifest duplicate raises first, so a second call stays
  a duplicate);
- :func:`register_google_mail_provider` / :func:`register_google_mail_tester`
  / :func:`register_google_mail_scope_options` /
  :func:`register_google_mail_tools` — wire the process-wide provider,
  tester, scope options, and tool registries the same explicit way;
  ``app.main`` calls the first two once at import and the tests call all
  four explicitly, importing this module alone stays free of side effects.

Everything here is import-safe: module imports perform **no** registration.
"""

from app.modules.catalog.manifest import ModuleManifest
from app.modules.catalog.registry import ModuleCatalog, register_default
from app.modules.google_mail.manifest import GOOGLE_MAIL_MANIFEST
from app.modules.google_mail.provider import GoogleGmailProvider, register_provider
from app.modules.google_mail.scope import google_mail_scope_options
from app.modules.google_mail.testers import google_mail_connection_tester
from app.modules.google_mail.tools import register_google_mail_tools
from app.modules.onboarding.options import register_scope_options_provider
from app.modules.onboarding.testers import register_tester
from app.modules.tools.registry import default_registry as tool_default_registry


def register_google_mail(catalog: ModuleCatalog) -> ModuleManifest:
    """Register the google_mail manifest on ``catalog`` and return it.

    Raises:
        ModuleRegistrationError: the catalog already holds ``google_mail`` or
            the manifest violates an invariant (same rules as any module).
    """
    catalog.register(GOOGLE_MAIL_MANIFEST)
    return GOOGLE_MAIL_MANIFEST


def register_default_google_mail() -> ModuleManifest:
    """Register google_mail on the process-wide default catalog and registries (production).

    Executed exactly once by the production import graph (``app.main``, the
    google-mail allowlist change): a second call raises
    ``ModuleRegistrationError``, which is the duplicate-registration
    contract. The manifest registers FIRST (its duplicate raises before
    anything else runs), then the G2 scope options provider and the G3 chat
    tools on the process-wide tool registry — both folds keep the reviewed
    ``app.main`` block to a single call per concern.
    """
    manifest = register_default(GOOGLE_MAIL_MANIFEST)
    register_scope_options_provider("google_mail", google_mail_scope_options)
    register_google_mail_tools(tool_default_registry)
    return manifest


def register_google_mail_provider() -> None:
    """Register the Google Gmail adapter factory under provider name ``google``.

    Process-wide and explicit, like every entry point here: importing this
    module registers nothing; ``app.main`` calls it once at import time.
    Overwrites an existing registration — same contract as ``register_tester``.
    """
    register_provider("google", GoogleGmailProvider)


def register_google_mail_tester() -> None:
    """Register :func:`google_mail_connection_tester` for the ``google_mail`` module.

    Same explicit, import-free contract as :func:`register_google_mail_provider`.
    """
    register_tester("google_mail", google_mail_connection_tester)


def register_google_mail_scope_options() -> None:
    """Register :func:`google_mail_scope_options` for the ``google_mail`` module.

    The dynamic ``select_scope`` choices (ALL + INBOX + the user's labels).
    Same explicit, import-free contract as the other entry points;
    :func:`register_default_google_mail` also runs it for production, and
    the onboarding tests call it after their registry-clearing fixtures.
    Overwrites an existing registration — same contract as ``register_tester``.
    """
    register_scope_options_provider("google_mail", google_mail_scope_options)


__all__ = [
    "register_default_google_mail",
    "register_google_mail",
    "register_google_mail_provider",
    "register_google_mail_scope_options",
    "register_google_mail_tester",
    "register_google_mail_tools",
]
