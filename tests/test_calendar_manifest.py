"""Calendar module manifest registration tests (local catalogs only).

The production import graph must NOT register the calendar module: the
process-wide allowlist test locks ``default_catalog`` empty until a reviewed
allowlist change lands (human decision on the calendar-mvp T1 slice). Every
registration entry point therefore lives behind explicit functions this file
exercises — against a local :class:`ModuleCatalog`, mirroring the local
fixture pattern of ``tests/test_catalog_registry.py``.

Contract under test (T1 / D4 / D7 / D8):

- ``register_calendar`` stores the manifest on the given catalog with the two
  onboarding steps (``connect_google`` redirect, then ``test_connection``),
  the fixed calendar.events scope, the three declared tool names, and the
  0.1.0 version — and never touches the process-wide catalog;
- ``register_default_calendar`` exists as code (the ``register_default`` call)
  but nothing in production executes it: even when invoked, the manifest is
  removed from ``default_catalog`` again, which is how the future allowlist
  review will adopt it.
"""

from app.modules.calendar.registry import register_calendar, register_default_calendar
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
    # The production catalog stays untouched by a local registration.
    assert "calendar" not in default_catalog.keys()


def test_default_catalog_registration_is_available_but_not_applied() -> None:
    """The register_default call exists as code; production never executes it."""
    try:
        manifest = register_default_calendar()
        assert default_catalog.get("calendar") is manifest
        assert "calendar" in default_catalog.keys()
    finally:
        # Roll back immediately: the allowlist test must stay order-independent.
        default_catalog._manifests.pop("calendar", None)
    assert "calendar" not in default_catalog.keys()
