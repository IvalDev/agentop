"""Declarative manifest of the ``google_mail`` module (Gmail MVP, google-mail M1–G3).

Pure data, no side effects: importing this module registers nothing. The
registration functions live in :mod:`app.modules.google_mail.registry`.

Manifest contract (developer-facing English; the web app renders its own
es-ES strings over these keys):

- two OAuth scopes (M1): ``gmail.modify`` (read + modify within the
  connected mailbox) and ``gmail.send`` (send on the user's behalf);
- six tool names (G3): the three free reads ``search_emails``,
  ``get_email``, ``list_labels`` and the three approval-gated actions
  ``send_email``, ``archive_message``, ``add_label`` (no delete tool);
- three onboarding steps (G2): ``connect_google`` (``redirect`` — the OAuth
  flow of :mod:`app.modules.oauth`), ``select_scope`` (``scope_select`` —
  whole mailbox vs. one label; its choices are dynamic, fetched from the
  stored connection's real Gmail labels through the module's scope options
  provider), then ``test_connection`` (verifies the stored tokens through
  the registered tester);
- an agent profile (agent-engine-routing): how the engine router sends
  Gmail requests to the mail engine, the scope semantics (whole mailbox vs
  one label), and that every action runs only after the user confirmed it
  (criterion 4) while reading stays free inside the scope.
"""

from app.modules.catalog.manifest import AgentProfile, ModuleManifest, StepSpec

#: The OAuth scopes the Gmail module declares (M1: modify + send).
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

#: The six chat tools the module contributes (G3: reads free, actions gated).
GOOGLE_MAIL_TOOLS = (
    "search_emails",
    "get_email",
    "list_labels",
    "send_email",
    "archive_message",
    "add_label",
)

#: How the chat agent's mail engine is routed and instructed (G3 completes it).
GOOGLE_MAIL_AGENT_PROFILE = AgentProfile(
    routing_description=(
        "The user's own email in Gmail: finding and reading messages inside the "
        "connected account and, with the user's confirmation, sending, archiving, "
        "and labeling them."
    ),
    instructions="""\
- Use only the Gmail tools, never the knowledge base.
- Never invent emails, senders, or threads: report only what the tools returned.
- Reading (search_emails, get_email, list_labels) needs no confirmation and stays inside \
the mailbox scope the user chose: with "All mail" you may search and read every message; \
with one label, search_emails only returns messages under that label and get_email is \
refused for messages outside it. search_emails returns message and thread ids: read a \
message with get_email before describing it. list_labels shows the mailbox's labels.
- Actions (send_email, archive_message, add_label) always wait for the user's approval \
and run only after the user confirmed it — never claim an action happened before the \
tool reports success. archive_message and add_label only act on messages inside the \
scope; send_email is not restricted by the scope.
- To reply inside a thread, pass the thread id to send_email.
- You cannot delete or trash email: no tool does it, and add_label refuses TRASH and SPAM.
- Confirm what was done, and report an out-of-scope refusal as the tool explained it.
""",
)

#: Registration record of the google_mail module (see the module docstring).
GOOGLE_MAIL_MANIFEST = ModuleManifest(
    key="google_mail",
    name="Gmail",
    description="Read and manage email on the user's connected Gmail account.",
    version="0.1.0",
    permissions=(GMAIL_MODIFY_SCOPE, GMAIL_SEND_SCOPE),
    tools=GOOGLE_MAIL_TOOLS,
    onboarding=(
        StepSpec(
            key="connect_google",
            type="redirect",
            title="Connect with Google",
            description="Authorize access to your Google Mail account.",
        ),
        StepSpec(
            key="select_scope",
            type="scope_select",
            title="Choose the mailbox scope",
            description="Read the whole mailbox or only one of its labels.",
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
