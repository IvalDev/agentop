"""Google Mail module manifest registration tests (google-mail G1/G2/G3).

The production import graph registers the google_mail module: ``app.main``
runs the reviewed allowlist change at import time, so ``default_catalog``
carries the manifest alongside the calendar one.

Contract under test (google-mail M1 / M2 / M3):

- ``register_google_mail`` stores the manifest on a given catalog with the
  three onboarding steps (``connect_google`` redirect, ``select_scope``
  mailbox choice, then ``test_connection``), the gmail.modify + gmail.send
  scopes, the six G3 tool names, and the 0.1.0 version — without
  disturbing the process-wide catalog;
- ``register_default_google_mail`` is executed exactly once by the
  production import graph, so a second call is a registration duplicate;
- the agent profile instructs the mail engine about scope semantics and
  that every action awaits the user's confirmation (criterion 4), and that
  ``search_emails`` already describes each hit while ``get_email`` is only
  for the full body (G3.1).
"""

import pytest

from app.modules.catalog.exceptions import ModuleRegistrationError
from app.modules.catalog.registry import ModuleCatalog, default_catalog
from app.modules.google_mail.manifest import GOOGLE_MAIL_MANIFEST
from app.modules.google_mail.registry import register_default_google_mail, register_google_mail

GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GOOGLE_MAIL_TOOLS = (
    "search_emails",
    "get_email",
    "list_labels",
    "send_email",
    "archive_message",
    "add_label",
)


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
    # G3: three free reads plus three approval-gated actions; no delete tool.
    assert manifest.tools == GOOGLE_MAIL_TOOLS
    assert not any("delete" in name or "trash" in name for name in manifest.tools)
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


def test_agent_profile_explains_scope_and_user_confirmation() -> None:
    """Criterion 4 in the prompt: actions await confirmation, reads do not.

    The instructions must state the scope semantics (whole mailbox vs one
    label), that every action runs only after the user confirmed it, and
    that sending is not restricted by the label scope.
    """
    profile = GOOGLE_MAIL_MANIFEST.agent
    assert profile is not None
    text = " ".join(profile.instructions.lower().split())

    assert "confirmed" in text
    assert "scope" in text
    assert "label" in text
    # Sending is exempt from the label filter but never from confirmation.
    assert "send" in text
    # Every tool is described by name; reads are free, and nothing deletes mail.
    for tool in GOOGLE_MAIL_TOOLS:
        assert tool in text, tool
    assert "needs no confirmation" in text
    assert "delete" in text and "trash" in text


def test_agent_profile_says_search_already_describes_each_hit() -> None:
    """G3.1: search_emails answers subject/sender/date/snippet; get_email is for the body.

    The old "read a message with get_email before describing it" rule would
    cost the model one extra tool round per hit, so it must be gone.
    """
    profile = GOOGLE_MAIL_MANIFEST.agent
    assert profile is not None
    text = " ".join(profile.instructions.lower().split())

    assert "subject, sender, date, and snippet" in text
    assert "full body" in text
    assert "before describing it" not in text


def test_production_registers_google_mail_once_at_import() -> None:
    """``app.main`` applies ``register_default_google_mail`` exactly once."""
    production = default_catalog.get("google_mail")

    assert production is not None
    assert production.key == "google_mail"
    assert production.tools == GOOGLE_MAIL_TOOLS
    # Importing again is a no-op for modules, so the duplicate must raise.
    with pytest.raises(ModuleRegistrationError):
        register_default_google_mail()
    # The failed duplicate leaves the production entry untouched.
    assert default_catalog.get("google_mail") is production


def test_agent_profile_calls_actions_without_asking_in_the_chat() -> None:
    """The approval notice confirms an action, so the agent never asks first in the chat.

    It asks only for missing details, shows a requested draft in the same
    answer as the tool call, and never leaves bracket placeholders in mail.
    """
    profile = GOOGLE_MAIL_MANIFEST.agent
    assert profile is not None
    text = " ".join(profile.instructions.lower().split())

    assert "approval notice" in text
    assert "never ask the user to confirm in the chat" in text
    assert "ask only for" in text
    assert "same answer" in text
    assert "placeholders" in text
