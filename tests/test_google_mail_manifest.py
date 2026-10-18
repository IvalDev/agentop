"""Google Mail module manifest registration tests (google-mail G1/G2).

The production import graph registers the google_mail module: ``app.main``
runs the reviewed allowlist change at import time, so ``default_catalog``
carries the manifest alongside the calendar one.

Contract under test (google-mail M1 / G2):

- ``register_google_mail`` stores the manifest on a given catalog with the
  three onboarding steps (``connect_google`` redirect, ``select_scope``
  mailbox choice, then ``test_connection``), the gmail.modify + gmail.send
  scopes, **no** tools (G3 fills them), and the 0.1.0 version — without
  disturbing the process-wide catalog;
- ``register_default_google_mail`` is executed exactly once by the
  production import graph, so a second call is a registration duplicate.
"""

import pytest

from app.modules.catalog.exceptions import ModuleRegistrationError
from app.modules.catalog.registry import ModuleCatalog, default_catalog
from app.modules.google_mail.manifest import GOOGLE_MAIL_MANIFEST
from app.modules.google_mail.registry import register_default_google_mail, register_google_mail

GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def test_google_mail_manifest_registers_on_a_local_catalog() -> None:
    """The local catalog gains the google_mail manifest with its full contract."""
    catalog = ModuleCatalog()

    manifest = register_google_mail(catalog)

    assert catalog.keys() == ["google_mail"]
    assert catalog.get("google_mail") is manifest
    assert manifest.key == "google_mail"
    assert manifest.name == "Gmail"
    assert manifest.description.strip()
    assert manifest.version == "0.1.0"
    assert manifest.permissions == (GMAIL_MODIFY_SCOPE, GMAIL_SEND_SCOPE)
    # Tools arrive with G3: G1 ships an empty tuple.
    assert manifest.tools == ()
    assert [(step.key, step.type) for step in manifest.onboarding] == [
        ("connect_google", "redirect"),
        ("select_scope", "scope_select"),
        ("test_connection", "test_connection"),
    ]
    assert all(step.title.strip() and step.description.strip() for step in manifest.onboarding)
    # A local registration shares the same manifest object as production,
    # which the import graph registers (reviewed allowlist change).
    assert "google_mail" in default_catalog.keys()
    assert default_catalog.get("google_mail") is manifest


def test_google_mail_declares_an_agent_profile_for_its_engine() -> None:
    """The mail engine stays off the knowledge base and never invents emails."""
    profile = GOOGLE_MAIL_MANIFEST.agent

    assert profile is not None
    routing = profile.routing_description.lower()
    assert "gmail" in routing or "email" in routing
    text = " ".join(profile.instructions.lower().split())
    assert "never the knowledge base" in text
    assert "never invent" in text
    # Registration on a fresh catalog also checks the profile's invariants.
    register_google_mail(ModuleCatalog())


def test_production_registers_google_mail_once_at_import() -> None:
    """``app.main`` applies ``register_default_google_mail`` exactly once."""
    production = default_catalog.get("google_mail")

    assert production is not None
    assert production.key == "google_mail"
    assert production.tools == ()
    # Importing again is a no-op for modules, so the duplicate must raise.
    with pytest.raises(ModuleRegistrationError):
        register_default_google_mail()
    # The failed duplicate leaves the production entry untouched.
    assert default_catalog.get("google_mail") is production
