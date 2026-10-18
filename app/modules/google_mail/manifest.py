"""Declarative manifest of the ``google_mail`` module (Gmail MVP, google-mail M1).

Pure data, no side effects: importing this module registers nothing. The
registration functions live in :mod:`app.modules.google_mail.registry`.

Manifest contract (developer-facing English; the web app renders its own
es-ES strings over these keys):

- two OAuth scopes (M1): ``gmail.modify`` (read + modify within the
  connected mailbox) and ``gmail.send`` (send on the user's behalf);
- no tools yet (G3 contributes the six read/action tools; names only then);
- two onboarding steps in G1: ``connect_google`` (``redirect`` — the OAuth
  flow of :mod:`app.modules.oauth`) followed by ``test_connection``
  (verifies the stored tokens through the registered tester). The
  ``scope_select`` step (whole mailbox vs. one label) is task G2 and must
  not appear here yet;
- an agent profile (agent-engine-routing): how the engine router sends
  Gmail requests to the mail engine and the minimal instructions G3 extends
  once the tools exist.
"""

from app.modules.catalog.manifest import AgentProfile, ModuleManifest, StepSpec

#: The OAuth scopes the Gmail module declares (M1: modify + send).
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

#: How the chat agent's mail engine is routed and instructed (minimal in G1;
#: G3 extends the instructions for the concrete tools).
GOOGLE_MAIL_AGENT_PROFILE = AgentProfile(
    routing_description=(
        "The user's own email in Gmail: finding and reading messages inside the "
        "connected account and, with the user's confirmation, sending, archiving, "
        "and labeling them."
    ),
    instructions="""\
- Use only the Gmail tools, never the knowledge base.
- Never invent emails, senders, or threads: report only what the tools returned.
- Reading stays inside the mailbox scope the user chose; any action (send, archive, \
label) runs only after the user confirmed it.
""",
)

#: Registration record of the google_mail module (see the module docstring).
GOOGLE_MAIL_MANIFEST = ModuleManifest(
    key="google_mail",
    name="Gmail",
    description="Read and manage email on the user's connected Gmail account.",
    version="0.1.0",
    permissions=(GMAIL_MODIFY_SCOPE, GMAIL_SEND_SCOPE),
    tools=(),
    onboarding=(
        StepSpec(
            key="connect_google",
            type="redirect",
            title="Connect with Google",
            description="Authorize access to your Google Mail account.",
        ),
        StepSpec(
            key="test_connection",
            type="test_connection",
            title="Verify the connection",
            description="Reads your Gmail profile to confirm access.",
        ),
    ),
    agent=GOOGLE_MAIL_AGENT_PROFILE,
)
