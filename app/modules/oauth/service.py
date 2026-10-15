"""OAuth authorization-code + PKCE mechanics (calendar-mvp D2).

Provider-neutral flow state; the Google endpoints are data constants here so
the router stays free of URLs:

- :func:`new_state` / :func:`new_pkce` — one-time ``state`` and the S256
  challenge pair for the authorization request;
- :func:`store_pending` / :func:`consume_pending` — the **one-shot** pending
  store keyed by ``state``: a callback may consume its entry exactly once
  (unknown, expired, and reused states are indistinguishable ``None``), and
  entries older than :data:`PENDING_TTL_SECONDS` are pruned on every store
  and consume. The record carries the *authenticated* :class:`CurrentUser`
  and the PKCE verifier: the callback is browser traffic without a bearer
  header, so the identity and tenant of the flow come from this record, never
  from the request;
- :func:`build_authorization_url` — the consent URL with ``S256`` PKCE,
  ``offline`` access, and forced consent (a refresh token is required to keep
  the connection healthy after the access token lapses);
- :func:`exchange_code` — POSTs the code + verifier to the token endpoint and
  parses the JSON into :class:`OAuthTokens`. A non-2xx response, a non-JSON
  body, or a response without an ``access_token`` raises
  :class:`TokenExchangeError` (the router maps it to 400); network failures
  propagate as :class:`httpx.RequestError` (mapped to 502), so the caller can
  tell "Google refused" from "Google is unreachable".

Single-process limitation (same contract as the tool registry and the default
module catalog): ``pending_states`` lives in this process's memory — a restart
or a multi-worker deployment invalidates in-flight flows, which the callback
surfaces as one fixed ``invalid or expired OAuth state`` 400.
"""

import base64
import hashlib
import secrets
import time
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from app.modules.identity.deps import CurrentUser

#: Google OAuth endpoints used by the flow (authorization + token exchange).
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

#: How long an authorization request may take to complete (start → callback).
PENDING_TTL_SECONDS = 600

#: Process-wide pending store, keyed by ``state`` (single-process MVP).
pending_states: dict[str, "PendingOAuth"] = {}


@dataclass(frozen=True)
class PendingOAuth:
    """One in-flight authorization request, waiting for its callback.

    Holds everything the unauthenticated callback needs: who started the flow
    (``user``, with ``tenant_id`` for the connection upsert), which module and
    onboarding step it serves, and the PKCE verifier only the token exchange
    may see. Never serialized, never logged.
    """

    user: CurrentUser
    module_key: str
    step_key: str
    pkce_verifier: str
    created_at: float


@dataclass(frozen=True)
class OAuthTokens:
    """Token-endpoint result: what a successful code exchange proves."""

    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scopes: tuple[str, ...]


class TokenExchangeError(Exception):
    """The token endpoint refused the exchange (or answered unusably)."""


def _now() -> float:
    """Wall-clock seconds; a seam so tests can age pending entries."""
    return time.time()


def _prune() -> None:
    """Drop pending entries past their TTL (runs on every store and consume)."""
    now = _now()
    expired = [
        state
        for state, pending in pending_states.items()
        if now - pending.created_at > PENDING_TTL_SECONDS
    ]
    for state in expired:
        del pending_states[state]


def new_state() -> str:
    """Return a fresh, unguessable ``state`` value (CSRF/replay protection)."""
    return secrets.token_urlsafe(32)


def new_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for one authorization request.

    The verifier follows RFC 7636 (43–128 unreserved characters); the
    challenge is its SHA-256, base64url-encoded without padding (S256).
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def store_pending(
    state: str,
    *,
    user: CurrentUser,
    module_key: str,
    step_key: str,
    pkce_verifier: str,
) -> None:
    """Remember one authorization request under ``state`` (prunes old entries)."""
    _prune()
    pending_states[state] = PendingOAuth(
        user=user,
        module_key=module_key,
        step_key=step_key,
        pkce_verifier=pkce_verifier,
        created_at=_now(),
    )


def consume_pending(state: str | None) -> PendingOAuth | None:
    """Pop the entry for ``state`` exactly once.

    Returns ``None`` for a missing, blank, or expired state — one indistin-
    guishable answer on purpose, so probing cannot tell "never existed" from
    "already used". The entry is removed even when it is expired.
    """
    if not state:
        return None
    _prune()
    pending = pending_states.pop(state, None)
    if pending is None:
        return None
    if _now() - pending.created_at > PENDING_TTL_SECONDS:
        return None
    return pending


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: Sequence[str],
    state: str,
    code_challenge: str,
) -> str:
    """Build Google's consent URL for one authorization request."""
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{query}"


async def exchange_code(
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    code_verifier: str,
    client: httpx.AsyncClient | None = None,
) -> OAuthTokens:
    """Exchange an authorization code for tokens (PKCE, server-side secret).

    ``client`` is an injection seam for tests (:class:`httpx.MockTransport`);
    without it this function owns a short-lived client and closes it.

    Raises:
        TokenExchangeError: non-2xx response, non-JSON body, or a response
            without an ``access_token`` — "Google said no".
        httpx.RequestError: the token endpoint could not be reached —
            "Google could not be asked" (the router answers 502).
    """
    form = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    if client is not None:
        response = await client.post(GOOGLE_TOKEN_ENDPOINT, data=form)
    else:
        async with httpx.AsyncClient() as owned_client:
            response = await owned_client.post(GOOGLE_TOKEN_ENDPOINT, data=form)
    if response.is_error:
        raise TokenExchangeError(f"token endpoint answered {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise TokenExchangeError("token endpoint answered non-JSON") from exc
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise TokenExchangeError("token response has no access_token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    scope = payload.get("scope")
    return OAuthTokens(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) and refresh_token else None,
        expires_in=int(expires_in) if isinstance(expires_in, (int, float)) else None,
        scopes=tuple(scope.split()) if isinstance(scope, str) else (),
    )


__all__ = [
    "GOOGLE_AUTHORIZATION_ENDPOINT",
    "GOOGLE_TOKEN_ENDPOINT",
    "PENDING_TTL_SECONDS",
    "OAuthTokens",
    "PendingOAuth",
    "TokenExchangeError",
    "build_authorization_url",
    "consume_pending",
    "exchange_code",
    "new_pkce",
    "new_state",
    "pending_states",
    "store_pending",
]
