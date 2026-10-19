"""Calendar module manifest registration tests.

The production import graph registers the calendar module: ``app.main`` runs
the reviewed allowlist change (human-approved 2026-10-15) at import time, so
``default_catalog`` carries the manifest alongside the process-wide allowlist
test in ``tests/test_catalog_registry.py``.

Contract under test (T1 / D4 / D7 / D8, allowlist update in calendar-mvp):

- ``register_calendar`` stores the manifest on a given catalog with the two
  onboarding steps (``connect_google`` redirect, then ``test_connection``),
  the fixed calendar.events scope, the three declared tool names, and the
  0.1.0 version — without disturbing the process-wide catalog;
- ``register_default_calendar`` is executed exactly once by the production
  import graph, so a second call is a registration duplicate.
"""

import pytest

from app.modules.calendar.manifest import CALENDAR_MANIFEST
from app.modules.calendar.registry import register_calendar, register_default_calendar
from app.modules.catalog.exceptions import ModuleRegistrationError
from app.modules.catalog.registry import ModuleCatalog, default_catalog

EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def test_calendar_manifest_registers_on_a_local_catalog() -> None:
    """The local catalog gains the calendar manifest with its full contract."""
    catalog = ModuleCatalog()

    manifest = register_calendar(catalog)

    assert catalog.keys() == ["calendar"]
    assert catalog.get("calendar") is manifest
    assert manifest.name == "Calendar"
    assert manifest.description.strip()
    assert manifest.version == "0.1.0"
    assert manifest.permissions == (EVENTS_SCOPE,)
    assert manifest.tools == ("list_events", "create_event", "cancel_event")
    assert [(step.key, step.type) for step in manifest.onboarding] == [
        ("connect_google", "redirect"),
        ("test_connection", "test_connection"),
    ]
    assert all(step.title.strip() and step.description.strip() for step in manifest.onboarding)
    # A local registration shares the same manifest object as production,
    # which the import graph registers (reviewed allowlist change).
    assert "calendar" in default_catalog.keys()
    assert default_catalog.get("calendar") is manifest


def test_calendar_declares_an_agent_profile_for_its_engine() -> None:
    """The calendar engine stays off the knowledge base and moves events safely."""
    profile = CALENDAR_MANIFEST.agent

    assert profile is not None
    routing = profile.routing_description.lower()
    assert "calendar" in routing
    assert all(word in routing for word in ("event", "availability", "cancel"))
    text = " ".join(profile.instructions.lower().split())
    assert "never the knowledge base" in text
    assert "list_events" in text
    assert "never invent events" in text
    assert "no update tool" in text
    create_first = text.index("first create the new event")
    cancel_after = text.index("cancel the old one only after the creation succeeded")
    assert create_first < cancel_after
    # Registration on a fresh catalog also checks the profile's invariants.
    register_calendar(ModuleCatalog())


def test_production_registers_the_calendar_once_at_import() -> None:
    """``app.main`` applies ``register_default_calendar`` exactly once."""
    production = default_catalog.get("calendar")

    assert production is not None
    assert production.key == "calendar"
    assert production.tools == ("list_events", "create_event", "cancel_event")
    # Importing again is a no-op for modules, so the duplicate must raise.
    with pytest.raises(ModuleRegistrationError):
        register_default_calendar()
    # The failed duplicate leaves the production entry untouched.
    assert default_catalog.get("calendar") is production
