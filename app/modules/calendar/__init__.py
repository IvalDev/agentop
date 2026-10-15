"""Calendar module: the first production module of the catalog (Google MVP).

Declares the module identity (:mod:`app.modules.calendar.manifest`), its
provider abstraction behind the ``CalendarProvider`` protocol
(:mod:`app.modules.calendar.provider`), and the connection tester for the
``test_connection`` onboarding step (:mod:`app.modules.calendar.testers`).

Registration deviation (human decision on the T1 slice, 2026-10-15): nothing
in the production import graph executes registration — importing this package
has no side effects. The manifest, provider, and tester register through the
explicit functions in :mod:`app.modules.calendar.registry`, which tests call
against local registries until a reviewed module-catalog allowlist change
wires them for production. Consequence until then: the production
``GET /api/v1/modules`` does not list ``calendar`` and no tenant can activate
it — intentional, pending that allowlist review.

The module's tools (``list_events``, ``create_event``, ``cancel_event``) are
implemented with the tasks that follow; this package starts with the manifest,
the provider protocol + Google adapter, and the tester.
"""
