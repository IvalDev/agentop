"""Declarative manifest of the ``calendar`` module (D4 / D7 / D8).

Pure data, no side effects: importing this module registers nothing. The
registration functions live in :mod:`app.modules.calendar.registry`.

Manifest contract (developer-facing English; the web app renders its own
es-ES strings over these keys):

- one fixed scope, ``calendar.events`` (D4 — CRUD over the user's own
  events, read included; no ``scope_select`` step in the MVP);
- three tool names contributed to the chat (T3 implements them; names only);
- two onboarding steps: ``connect_google`` (``redirect`` — the OAuth flow of
  :mod:`app.modules.oauth`) followed by ``test_connection`` (verifies the
  stored tokens through the registered tester).
"""

from app.modules.catalog.manifest import ModuleManifest, StepSpec

#: The single OAuth scope the calendar tools need (D4).
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"

#: Registration record of the calendar module (see the module docstring).
CALENDAR_MANIFEST = ModuleManifest(
    key="calendar",
    name="Calendar",
    description="Read, create, and cancel events on the user's connected Google Calendar.",
    version="0.1.0",
    permissions=(CALENDAR_EVENTS_SCOPE,),
    tools=("list_events", "create_event", "cancel_event"),
    onboarding=(
        StepSpec(
            key="connect_google",
            type="redirect",
            title="Connect with Google",
            description="Authorize access to your Google Calendar account.",
        ),
        StepSpec(
            key="test_connection",
            type="test_connection",
            title="Verify the connection",
            description="Lists one event from your primary calendar to confirm access.",
        ),
    ),
)
