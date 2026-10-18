"""Google Mail module: Gmail access for the agent (Google MVP).

Declares the module identity (:mod:`app.modules.google_mail.manifest`), its
provider abstraction behind the ``GmailProvider`` protocol
(:mod:`app.modules.google_mail.provider`), and the connection tester for the
``test_connection`` onboarding step (:mod:`app.modules.google_mail.testers`).

Importing this package has no side effects: the manifest, provider, and
tester register through the explicit functions in
:mod:`app.modules.google_mail.registry`, which ``app.main`` executes once at
import time (reviewed google-mail allowlist change, G1).

G1 ships the module base only: manifest (gmail.modify + gmail.send scopes,
``redirect`` + ``test_connection`` onboarding steps), provider protocol with
the single ``get_profile`` call, and the tester. The mailbox/label choice
(``scope_select``), the read/action tools, and the web chrome arrive with
the tasks that follow.
"""
