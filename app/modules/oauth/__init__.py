"""Provider-neutral OAuth connections package (calendar-mvp D2).

Owns the authorization-code + PKCE mechanics a module connection needs before
its tokens reach :mod:`app.modules.connections`:

- :mod:`app.modules.oauth.service` — state and PKCE generation, the one-shot
  pending store, the authorization URL, and the token exchange;
- :mod:`app.modules.oauth.router` — the start endpoint behind authentication
  and the unauthenticated Google callback.

Importing this package has no side effects: no catalog registration, no
router mounting — :mod:`app.main` wires the router explicitly, and the
calendar module's registration stays behind the human-decided allowlist gate
(see :mod:`app.modules.calendar`).
"""
