"""Google Mail module: Gmail access for the agent (Google MVP).

Declares the module identity (:mod:`app.modules.google_mail.manifest`), its
provider abstraction behind the ``GmailProvider`` protocol
(:mod:`app.modules.google_mail.provider`), the connection tester for the
``test_connection`` onboarding step (:mod:`app.modules.google_mail.testers`),
and the mailbox scope selection of G2 (:mod:`app.modules.google_mail.scope`).

Importing this package has no side effects: the manifest, provider, tester,
and scope options provider register through the explicit functions in
:mod:`app.modules.google_mail.registry`, which ``app.main`` executes once at
import time (reviewed google-mail allowlist change, G1/G2).

G2 ships the ``select_scope`` step on top of the G1 base: its choices come
from the user's real Gmail labels (``users.labelsList``) through the module's
scope options provider, exactly one choice (whole mailbox or one label),
persisted by the engine and read back through
:func:`get_effective_scope <app.modules.google_mail.scope.get_effective_scope>`
for the read/action tools that follow.
"""
