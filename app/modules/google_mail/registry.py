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
  a second call raises ``ModuleRegistrationError``;
- :func:`register_google_mail_provider` / :func:`register_google_mail_tester`
  — wire the process-wide provider and tester registries the same explicit
  way; ``app.main`` calls them once at import, importing this module alone
  stays free of side effects.

Everything here is import-safe: module imports perform **no** registration.
"""

from app.modules.catalog.manifest import ModuleManifest
from app.modules.catalog.registry import ModuleCatalog, register_default
from app.modules.google_mail.manifest import GOOGLE_MAIL_MANIFEST
from app.modules.google_mail.provider import GoogleGmailProvider, register_provider
from app.modules.google_mail.testers import google_mail_connection_tester
from app.modules.onboarding.testers import register_tester


def register_google_mail(catalog: ModuleCatalog) -> ModuleManifest:
    """Register the google_mail manifest on ``catalog`` and return it.

    Raises:
        ModuleRegistrationError: the catalog already holds ``google_mail`` or
            the manifest violates an invariant (same rules as any module).
    """
    catalog.register(GOOGLE_MAIL_MANIFEST)
    return GOOGLE_MAIL_MANIFEST


def register_default_google_mail() -> ModuleManifest:
    """Register the google_mail manifest on the process-wide default catalog.

    Executed exactly once by the production import graph (``app.main``, the
    google-mail G1 allowlist change): a second call raises
    ``ModuleRegistrationError``, which is the duplicate-registration contract.
    """
    return register_default(GOOGLE_MAIL_MANIFEST)


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


__all__ = [
    "register_default_google_mail",
    "register_google_mail",
    "register_google_mail_provider",
    "register_google_mail_tester",
]
