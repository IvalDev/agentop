"""Mailbox scope selection of the google_mail module (G2, google-mail M2).

Two halves of one contract:

- :func:`google_mail_scope_options` — the module's *scope options provider*
  (the engine hook of :mod:`app.modules.onboarding.options`): it builds the
  ``select_scope`` step's choices from the user's REAL Gmail labels
  (``users.labelsList`` over the stored connection's access token): a fixed
  ``ALL`` option, the system label ``INBOX``, then the user's own labels in
  API order, exactly one choice (MVP: whole mailbox OR one label), each
  with its display label (``ALL``/``INBOX`` by their fixed names, a user
  label by its Gmail ``name``). It
  mirrors the calendar tools' token precheck: newest connection wins, an
  expired access token is refreshed through the oauth service and the new
  blob is persisted on the same connection, and every unusable situation —
  no connection, unreadable blob, no usable token, an unrefreshable grant,
  an unknown provider, a failed labels call — raises
  :class:`ScopeOptionsError` so the engine fails closed (never a 500,
  never a token leak);
- :func:`get_effective_scope` — the *G3 accessor*: it reads the selection
  the engine persisted in ``OnboardingState.data["scopes"]`` (one answer,
  the engine's answer machinery) plus the stored connection and answers a
  :class:`ScopeSelection` (id + display name) — or ``None`` for the
  NOT_CONFIGURED semantics G3 maps in its tool precheck: no completed
  selection, a malformed/multiple selection, or a connection whose secret
  was destroyed (revoked).

Option values are the opaque strings the contract carries and persists:
``ALL``, the label id ``INBOX``, and user label ids (``Label_7``). Display
names ride beside them as :attr:`ScopeOptions.labels` (G4, display only):
the fixed :data:`_SCOPE_NAMES` for ``ALL``/``INBOX`` — the API names system
labels by their id, so its ``INBOX`` name is never shown — and the Gmail
``name`` of each user label as-is (a label without a usable name gets no
display label, so the renderer shows its id). The accessor reuses the same
fixed names for the agent's spoken output.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import (
    CredentialsKeyFormatError,
    DecryptionError,
    decrypt_credentials,
)
from app.modules.catalog.registry import ModuleCatalog
from app.modules.connections.models import UserConnection
from app.modules.connections.service import create_or_update
from app.modules.google_mail.manifest import GOOGLE_MAIL_MANIFEST
from app.modules.google_mail.provider import get_provider
from app.modules.oauth import service as oauth_service
from app.modules.onboarding.models import OnboardingState
from app.modules.onboarding.options import (
    ScopeOptions,
    ScopeOptionsContext,
    ScopeOptionsError,
)

logger = logging.getLogger(__name__)

#: The module key both stored rows (connection + onboarding state) carry.
MODULE_KEY = "google_mail"

#: The one option meaning "the whole mailbox" (not a label id).
ALL_SCOPE = "ALL"

#: The system label always offered after ``ALL`` (Gmail always has one).
INBOX_SCOPE = "INBOX"

#: Same refresh skew the calendar precheck uses (never refresh a live token).
EXPIRY_SKEW_SECONDS = 60

#: Display names for the two fixed choices (the scope picker and the agent's output).
_SCOPE_NAMES = {ALL_SCOPE: "All mail", INBOX_SCOPE: "Inbox"}


@dataclass(frozen=True)
class ScopeSelection:
    """The persisted mailbox scope: its id and a human-readable name."""

    scope_id: str
    scope_name: str


async def _load_connection(
    session: AsyncSession, *, tenant_id: int, user_id: int
) -> UserConnection | None:
    """The caller's newest ``google_mail`` connection row (``None`` when absent)."""
    result = await session.execute(
        select(UserConnection)
        .where(
            UserConnection.tenant_id == tenant_id,
            UserConnection.user_id == user_id,
            UserConnection.module_key == MODULE_KEY,
        )
        .order_by(UserConnection.created_at.desc(), UserConnection.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _decrypt_blob(credentials_key: str | None, row: UserConnection) -> dict[str, Any] | None:
    """Decrypt the row's credentials JSON; ``None`` when absent or unreadable.

    Same collapse rule as the calendar precheck: a missing key, a malformed
    key, corrupted ciphertext, or a non-object blob all answer ``None`` and
    never echo the underlying error.
    """
    if credentials_key is None or row.credentials_encrypted is None:
        return None
    try:
        blob: Any = json.loads(decrypt_credentials(credentials_key, row.credentials_encrypted))
    except (CredentialsKeyFormatError, DecryptionError, json.JSONDecodeError):
        return None
    return blob if isinstance(blob, dict) else None


def _is_expired(expires_at: Any) -> bool:
    """True when ``expires_at`` (epoch seconds) is a number past the skew.

    A blob without a parsable ``expires_at`` is treated as *not* expired:
    the provider's own 401 is the fallback (never guess a refresh).
    """
    if not isinstance(expires_at, (int, float)):
        return False
    return time.time() >= float(expires_at) - EXPIRY_SKEW_SECONDS


def _google_client_credentials(settings: Settings | None) -> tuple[str, str] | None:
    """The configured Google client id/secret, or ``None`` when unusable."""
    if settings is None:
        return None
    client_id = settings.google_client_id.get_secret_value() if settings.google_client_id else ""
    client_secret = (
        settings.google_client_secret.get_secret_value() if settings.google_client_secret else ""
    )
    if not client_id or not client_secret:
        return None
    return client_id, client_secret


def _mail_catalog() -> ModuleCatalog:
    """A throwaway catalog holding only ``google_mail`` for the refresh upsert.

    ``create_or_update`` validates its ``module_key`` against a catalog; the
    precheck brings its own one-module view instead of depending on the
    process-wide default (same pattern as the calendar refresh).
    """
    catalog = ModuleCatalog()
    catalog.register(GOOGLE_MAIL_MANIFEST)
    return catalog


async def _refresh_and_persist(
    context: ScopeOptionsContext, *, row: UserConnection, blob: dict[str, Any]
) -> str:
    """Refresh the access token and store the new blob on the same connection.

    Raises:
        ScopeOptionsError: no client credentials, no refresh token, or a
            refused/unreachable token endpoint — the "reconnect" situation.
    """
    credentials = _google_client_credentials(context.settings)
    if credentials is None:
        raise ScopeOptionsError("Google client credentials are not configured")
    client_id, client_secret = credentials
    refresh_token = blob.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ScopeOptionsError("the stored connection has no refresh token")
    try:
        tokens = await oauth_service.refresh_access_token(
            refresh_token=refresh_token, client_id=client_id, client_secret=client_secret
        )
    except (oauth_service.TokenExchangeError, httpx.RequestError):
        logger.warning(
            "google_mail token refresh failed; the user must reconnect",
            exc_info=True,
            extra={"module_key": MODULE_KEY},
        )
        raise ScopeOptionsError("Google refused to refresh the access token") from None
    refreshed: dict[str, Any] = {
        "access_token": tokens.access_token,
        # Google may omit the refresh token on refresh: keep the stored one.
        "refresh_token": tokens.refresh_token or refresh_token,
        "scopes": list(tokens.scopes) or list(blob.get("scopes") or []),
    }
    if tokens.expires_in is not None:
        refreshed["expires_at"] = int(time.time()) + tokens.expires_in
    new_blob = json.dumps(refreshed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    await create_or_update(
        context.session,
        catalog=_mail_catalog(),
        credentials_key=context.credentials_key,
        caller=context.caller,
        module_key=MODULE_KEY,
        provider=row.provider,
        credentials=new_blob,
    )
    return tokens.access_token


def _build_options(labels: Any) -> ScopeOptions:
    """Fold the provider's label list into the exclusive choice tuple.

    ``ALL`` then ``INBOX`` are fixed (named by :data:`_SCOPE_NAMES`); only
    ``type == "user"`` labels follow, in API order, deduplicated against
    what is already offered, each named by its non-empty Gmail ``name``
    (no usable name: no display label, the id shows). Anything malformed in
    the list is skipped — a malformed answer as a whole (not a list) is the
    caller's fail-closed case, not this fold.
    """
    options: list[str] = [ALL_SCOPE, INBOX_SCOPE]
    names: dict[str, str] = dict(_SCOPE_NAMES)
    if isinstance(labels, list):
        for label in labels:
            if not isinstance(label, dict) or label.get("type") != "user":
                continue
            label_id = label.get("id")
            if isinstance(label_id, str) and label_id and label_id not in options:
                options.append(label_id)
                name = label.get("name")
                if isinstance(name, str) and name:
                    names[label_id] = name
    return ScopeOptions(options=tuple(options), exclusive=True, labels=names)


async def google_mail_scope_options(context: ScopeOptionsContext) -> ScopeOptions:
    """The module's scope options provider (engine hook, ``select_scope``).

    Raises:
        ScopeOptionsError: no stored connection, an unreadable blob, no
            usable access token, an unrefreshable expired grant, an unknown
            provider, or a failed labels call — the engine fails closed.
    """
    row = await _load_connection(
        context.session, tenant_id=context.caller.tenant_id, user_id=context.caller.id
    )
    if row is None:
        raise ScopeOptionsError("no stored Gmail connection")
    blob = _decrypt_blob(context.credentials_key, row)
    if blob is None:
        raise ScopeOptionsError("the stored Gmail connection is unreadable")
    access_token = blob.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ScopeOptionsError("the stored Gmail connection has no access token")
    if _is_expired(blob.get("expires_at")):
        access_token = await _refresh_and_persist(context, row=row, blob=blob)
    try:
        factory = get_provider(row.provider)
        provider = factory(access_token=access_token)
        labels = await provider.list_labels()
    except Exception as exc:
        raise ScopeOptionsError("Gmail labels could not be listed") from exc
    if not isinstance(labels, list):
        raise ScopeOptionsError("Gmail labels answer was not a list")
    return _build_options(labels)


def _selection_from_data(data: Any) -> ScopeSelection | None:
    """The one valid persisted selection in ``state.data``, or ``None``.

    Anything not shaped as exactly one non-empty string under ``scopes`` —
    missing, empty, multiple, non-string — answers ``None`` (malformed data
    is never trusted as configured).
    """
    if not isinstance(data, dict):
        return None
    scopes = data.get("scopes")
    if not isinstance(scopes, list) or len(scopes) != 1:
        return None
    scope_id = scopes[0]
    if not isinstance(scope_id, str) or not scope_id:
        return None
    return ScopeSelection(scope_id=scope_id, scope_name=_SCOPE_NAMES.get(scope_id, scope_id))


async def get_effective_scope(
    session: AsyncSession, *, tenant_id: int, user_id: int
) -> ScopeSelection | None:
    """The caller's persisted mailbox scope, or ``None`` (NOT_CONFIGURED).

    The selection lives in the onboarding state (the engine's answer
    machinery persisted it at ``select_scope``); it only counts when a
    stored connection still holds its secret too — revoke destroys that
    blob, so a stale selection answers ``None``. Never decrypts, never
    refreshes, never raises for missing data: this is G3's cheap precheck.
    """
    state = (
        await session.execute(
            select(OnboardingState).where(
                OnboardingState.tenant_id == tenant_id,
                OnboardingState.user_id == user_id,
                OnboardingState.module_key == MODULE_KEY,
            )
        )
    ).scalar_one_or_none()
    if state is None:
        return None
    selection = _selection_from_data(state.data)
    if selection is None:
        return None
    row = await _load_connection(session, tenant_id=tenant_id, user_id=user_id)
    if row is None or row.credentials_encrypted is None:
        return None
    return selection


__all__ = [
    "ALL_SCOPE",
    "ScopeSelection",
    "get_effective_scope",
    "google_mail_scope_options",
]
