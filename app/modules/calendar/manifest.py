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
  stored tokens through the registered tester);
- an agent profile (agent-engine-routing D3): what the engine router sends
  to the calendar engine and how that engine uses the three tools (there is
  no update tool, so a move is create-then-cancel).
"""

from app.modules.catalog.manifest import AgentProfile, ModuleManifest, StepSpec

#: The single OAuth scope the calendar tools need (D4).
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"

#: How the chat agent's calendar engine is routed and instructed.
CALENDAR_AGENT_PROFILE = AgentProfile(
    routing_description=(
        "The user's own calendar: listing or finding events, checking the schedule and "
        "availability, and creating, moving (rescheduling), or cancelling appointments "
        "and meetings."
    ),
    instructions="""\
- Use only the calendar tools, never the knowledge base.
- Resolve relative dates ("today", "tomorrow", "the 20th") against the current date and \
timezone stated in this system message.
- To find an event, call list_events over a range that covers the whole requested day or \
days in the user's timezone.
- Pass times to the tools without a UTC offset; they are read in the user's timezone.
- Never invent events: report only what the tools returned.
- Cancel only event ids returned by list_events.
- There is no update tool. To move an event, first create the new event with the same \
title and description, and cancel the old one only after the creation succeeded.
- Confirm what was done with the date and the local time.
""",
)

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
    agent=CALENDAR_AGENT_PROFILE,
)
