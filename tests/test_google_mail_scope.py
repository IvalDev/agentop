"""Gmail scope selection: labels provider, options hook, effective scope (G2).

Contract under test (google-mail M2):

- :meth:`GoogleGmailProvider.list_labels` calls the Gmail API
  ``users.labelsList`` endpoint (``GET …/users/me/labels``) with a Bearer
  header and returns the ``labels`` payload; 401/403 maps to
  :class:`GmailAuthenticationError`, any other failure (non-2xx, non-JSON,
  missing ``labels``) to :class:`GmailProviderError`;
- :func:`google_mail_scope_options` — the module's engine hook — builds the
  ``scope_select`` options from the user's real labels through the stored
  connection: a fixed ``ALL`` option, the system label ``INBOX``, then the
  user's own labels (id order preserved); the result is exclusive (MVP: one
  label or the whole mailbox). A missing/unreadable connection, an
  unrefreshable expired token, an unknown provider, or a failed labels call
  all raise :class:`ScopeOptionsError` (the engine fails closed — never a
  500, never a token leak). An expired token is refreshed with the oauth
  service and the new blob persisted on the same connection (the calendar
  precheck mechanism, mirrored);
- :func:`get_effective_scope` reads the engine-persisted selection
  (``OnboardingState.data["scopes"]``) and answers a
  :class:`ScopeSelection` (id + display name) only when a usable stored
  connection exists too; anything else is ``None`` — the NOT_CONFIGURED
  semantic G3 maps in its precheck.
"""

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.crypto import decrypt_credentials, encrypt_credentials, generate_credentials_key
from app.core.db import Database
from app.modules.connections.models import ConnectionStatus, UserConnection
from app.modules.google_mail.manifest import GOOGLE_MAIL_MANIFEST
from app.modules.google_mail.provider import (
    GOOGLE_GMAIL_LABELS_URL,
    GmailAuthenticationError,
    GmailProviderError,
    GoogleGmailProvider,
    provider_registry,
)
from app.modules.google_mail.scope import (
    ALL_SCOPE,
    get_effective_scope,
    google_mail_scope_options,
)
from app.modules.identity.deps import CurrentUser
from app.modules.identity.models import UserRole
from app.modules.oauth import service as oauth_service
from app.modules.onboarding.models import OnboardingState, OnboardingStatus
from app.modules.onboarding.options import ScopeOptionsContext, ScopeOptionsError

ACCESS_TOKEN = "ya29.test-access-token"
REFRESH_TOKEN = "1//test-refresh-token"
JWT_SECRET = "test-secret-0123456789-abcdefghijklmnop"
GOOGLE_CLIENT_ID = "client.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "GOCSPX-test-client-secret"

LABELS_PAYLOAD = {
    "labels": [
        {"id": "INBOX", "name": "Inbox", "type": "system"},
        {"id": "SENT", "name": "Sent", "type": "system"},
        {"id": "Label_7", "name": "Asistente", "type": "user"},
        {"id": "Label_9", "name": "Facturas", "type": "user"},
    ]
}


@pytest.fixture(autouse=True)
def registries_clean() -> AsyncIterator[None]:
    """The process-wide Gmail provider registry starts and ends empty per test."""
    provider_registry.clear()
    yield
    provider_registry.clear()


@pytest.fixture
def oauth_key() -> str:
    return generate_credentials_key()


@pytest.fixture
def google_settings(oauth_key: str) -> Settings:
    return Settings(
        jwt_secret=JWT_SECRET,
        access_token_expire_minutes=30,
        credentials_key=oauth_key,
        google_client_id=GOOGLE_CLIENT_ID,
        google_client_secret=GOOGLE_CLIENT_SECRET,
    )


def _owner_principal() -> CurrentUser:
    return CurrentUser(
        id=1,
        tenant_id=1,
        email="owner@acme.test",
        full_name="Acme Owner",
        role=UserRole.OWNER,
        is_superadmin=False,
    )


async def _store_connection(
    db: Database,
    oauth_key: str,
    blob: dict[str, Any] | None,
    *,
    status: ConnectionStatus = ConnectionStatus.CONNECTED,
) -> None:
    encrypted = None if blob is None else encrypt_credentials(oauth_key, json.dumps(blob))
    async with db.sessionmaker() as session:
        session.add(
            UserConnection(
                tenant_id=1,
                user_id=1,
                module_key="google_mail",
                provider="google",
                status=status,
                credentials_encrypted=encrypted,
                metadata_={},
            )
        )
        await session.commit()


async def _stored_blob(db: Database, oauth_key: str) -> dict[str, Any] | None:
    async with db.sessionmaker() as session:
        row = (
            await session.execute(
                select(UserConnection).where(UserConnection.module_key == "google_mail")
            )
        ).scalar_one_or_none()
    if row is None or row.credentials_encrypted is None:
        return None
    return json.loads(decrypt_credentials(oauth_key, row.credentials_encrypted))


def _context(session: Any, oauth_key: str, settings: Settings | None) -> ScopeOptionsContext:
    return ScopeOptionsContext(
        caller=_owner_principal(),
        session=session,
        module_key="google_mail",
        credentials_key=oauth_key,
        settings=settings,
    )


# --- list_labels (the provider seam G1 created) ------------------------------


def test_google_adapter_satisfies_the_protocol_with_list_labels() -> None:
    provider = GoogleGmailProvider(ACCESS_TOKEN)
    assert hasattr(provider, "list_labels")


async def test_list_labels_sends_bearer_and_returns_the_payload() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["authorization"] = request.headers["Authorization"]
        return httpx.Response(200, json=LABELS_PAYLOAD)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        labels = await provider.list_labels()

    assert labels == LABELS_PAYLOAD["labels"]
    assert seen["method"] == "GET"
    assert seen["url"] == GOOGLE_GMAIL_LABELS_URL
    assert seen["authorization"] == f"Bearer {ACCESS_TOKEN}"


@pytest.mark.parametrize("status", [401, 403])
async def test_list_labels_maps_auth_statuses_to_authentication_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"status": "UNAUTHENTICATED"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailAuthenticationError):
            await provider.list_labels()


async def test_list_labels_maps_other_errors_to_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError):
            await provider.list_labels()


async def test_list_labels_rejects_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="non-JSON"):
            await provider.list_labels()


async def test_list_labels_requires_the_labels_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"profile": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleGmailProvider(ACCESS_TOKEN, client=client)
        with pytest.raises(GmailProviderError, match="labels"):
            await provider.list_labels()


# --- google_mail_scope_options (the engine hook) -----------------------------


class _FakeGmailProvider:
    """Provider double: fixed labels, records the token it was built with."""

    def __init__(self, access_token: str, *, labels: list[dict[str, Any]] | Exception = None):
        self.access_token = access_token
        self._labels = labels if labels is not None else LABELS_PAYLOAD["labels"]

    async def get_profile(self) -> dict[str, Any]:
        return {"emailAddress": "agent@acme.test"}

    async def list_labels(self) -> list[dict[str, Any]]:
        if isinstance(self._labels, Exception):
            raise self._labels
        return self._labels


def _register_fake_provider(
    *, labels: list[dict[str, Any]] | Exception = None, captured: dict[str, Any] | None = None
) -> None:
    def factory(access_token: str) -> _FakeGmailProvider:
        if captured is not None:
            captured["access_token"] = access_token
        return _FakeGmailProvider(access_token, labels=labels)

    provider_registry["google"] = factory


def _fresh_blob() -> dict[str, Any]:
    return {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "expires_at": int(time.time()) + 3600,
        "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
    }


async def test_scope_options_are_all_then_inbox_then_user_labels(
    seeded_db: Database, oauth_key: str, google_settings: Settings
) -> None:
    await _store_connection(seeded_db, oauth_key, _fresh_blob())
    captured: dict[str, Any] = {}
    _register_fake_provider(captured=captured)

    async with seeded_db.sessionmaker() as session:
        options = await google_mail_scope_options(_context(session, oauth_key, google_settings))

    assert options.options == (ALL_SCOPE, "INBOX", "Label_7", "Label_9")
    assert options.exclusive is True
    assert captured["access_token"] == ACCESS_TOKEN


async def test_scope_options_without_a_connection_raise(
    seeded_db: Database, oauth_key: str
) -> None:
    async with seeded_db.sessionmaker() as session:
        with pytest.raises(ScopeOptionsError):
            await google_mail_scope_options(
                _context(session, oauth_key, Settings(jwt_secret=JWT_SECRET))
            )


async def test_scope_options_with_an_unreadable_blob_raise(
    seeded_db: Database, oauth_key: str
) -> None:
    async with seeded_db.sessionmaker() as session:
        session.add(
            UserConnection(
                tenant_id=1,
                user_id=1,
                module_key="google_mail",
                provider="google",
                status=ConnectionStatus.CONNECTED,
                credentials_encrypted=None,
                metadata_={},
            )
        )
        await session.commit()
        with pytest.raises(ScopeOptionsError):
            await google_mail_scope_options(
                _context(session, oauth_key, Settings(jwt_secret=JWT_SECRET))
            )


async def test_scope_options_with_a_live_token_never_refresh(
    seeded_db: Database, oauth_key: str, google_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _store_connection(seeded_db, oauth_key, _fresh_blob())
    _register_fake_provider()

    async def boom(**kwargs: Any) -> oauth_service.OAuthTokens:
        raise AssertionError("a live token must not trigger a refresh")

    monkeypatch.setattr(oauth_service, "refresh_access_token", boom)
    async with seeded_db.sessionmaker() as session:
        options = await google_mail_scope_options(_context(session, oauth_key, google_settings))

    assert options.options[0] == ALL_SCOPE


async def test_scope_options_refresh_an_expired_token_and_persist_it(
    seeded_db: Database, oauth_key: str, google_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    expired = _fresh_blob()
    expired["expires_at"] = int(time.time()) - 120
    await _store_connection(seeded_db, oauth_key, expired)
    captured: dict[str, Any] = {}
    _register_fake_provider(captured=captured)

    async def fake_refresh(**kwargs: Any) -> oauth_service.OAuthTokens:
        return oauth_service.OAuthTokens(
            access_token="ya29.fresh", refresh_token=None, expires_in=3600, scopes=()
        )

    monkeypatch.setattr(oauth_service, "refresh_access_token", fake_refresh)
    async with seeded_db.sessionmaker() as session:
        options = await google_mail_scope_options(_context(session, oauth_key, google_settings))

    assert captured["access_token"] == "ya29.fresh"
    stored = await _stored_blob(seeded_db, oauth_key)
    assert stored is not None
    assert stored["access_token"] == "ya29.fresh"
    # Google may omit the refresh token on refresh: the stored one is kept.
    assert stored["refresh_token"] == REFRESH_TOKEN
    assert options.options[0] == ALL_SCOPE


async def test_scope_options_with_an_unrefreshable_token_raise(
    seeded_db: Database, oauth_key: str, google_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    expired = _fresh_blob()
    expired["expires_at"] = int(time.time()) - 120
    await _store_connection(seeded_db, oauth_key, expired)

    async def refused(**kwargs: Any) -> oauth_service.OAuthTokens:
        raise oauth_service.TokenExchangeError("invalid_grant")

    monkeypatch.setattr(oauth_service, "refresh_access_token", refused)
    async with seeded_db.sessionmaker() as session:
        with pytest.raises(ScopeOptionsError):
            await google_mail_scope_options(_context(session, oauth_key, google_settings))


async def test_scope_options_when_the_labels_call_fails_raise(
    seeded_db: Database, oauth_key: str, google_settings: Settings
) -> None:
    await _store_connection(seeded_db, oauth_key, _fresh_blob())
    _register_fake_provider(labels=GmailAuthenticationError("Gmail API answered 401"))

    async with seeded_db.sessionmaker() as session:
        with pytest.raises(ScopeOptionsError):
            await google_mail_scope_options(_context(session, oauth_key, google_settings))


# --- get_effective_scope (the G3 accessor) -----------------------------------


async def _store_state(db: Database, data: dict[str, Any]) -> None:
    async with db.sessionmaker() as session:
        session.add(
            OnboardingState(
                tenant_id=1,
                user_id=1,
                module_key="google_mail",
                status=OnboardingStatus.COMPLETED,
                current_step=None,
                data=data,
                secrets_encrypted=None,
            )
        )
        await session.commit()


async def test_get_effective_scope_without_a_state_is_not_configured(
    seeded_db: Database,
) -> None:
    async with seeded_db.sessionmaker() as session:
        assert await get_effective_scope(session, tenant_id=1, user_id=1) is None


async def test_get_effective_scope_without_a_connection_is_not_configured(
    seeded_db: Database,
) -> None:
    await _store_state(seeded_db, {"scopes": [ALL_SCOPE]})
    async with seeded_db.sessionmaker() as session:
        assert await get_effective_scope(session, tenant_id=1, user_id=1) is None


async def test_get_effective_scope_all_mail(seeded_db: Database, oauth_key: str) -> None:
    await _store_state(seeded_db, {"scopes": [ALL_SCOPE]})
    await _store_connection(seeded_db, oauth_key, _fresh_blob())
    async with seeded_db.sessionmaker() as session:
        selection = await get_effective_scope(session, tenant_id=1, user_id=1)
    assert selection is not None
    assert (selection.scope_id, selection.scope_name) == (ALL_SCOPE, "All mail")


async def test_get_effective_scope_system_label(seeded_db: Database, oauth_key: str) -> None:
    await _store_state(seeded_db, {"scopes": ["INBOX"]})
    await _store_connection(seeded_db, oauth_key, _fresh_blob())
    async with seeded_db.sessionmaker() as session:
        selection = await get_effective_scope(session, tenant_id=1, user_id=1)
    assert selection is not None
    assert (selection.scope_id, selection.scope_name) == ("INBOX", "Inbox")


async def test_get_effective_scope_user_label(seeded_db: Database, oauth_key: str) -> None:
    await _store_state(seeded_db, {"scopes": ["Label_7"]})
    await _store_connection(seeded_db, oauth_key, _fresh_blob())
    async with seeded_db.sessionmaker() as session:
        selection = await get_effective_scope(session, tenant_id=1, user_id=1)
    assert selection is not None
    assert selection.scope_id == "Label_7"
    assert selection.scope_name == "Label_7"


async def test_get_effective_scope_ignores_malformed_selections(
    seeded_db: Database, oauth_key: str
) -> None:
    await _store_state(seeded_db, {})
    await _store_connection(seeded_db, oauth_key, _fresh_blob())
    for data in (
        {},
        {"scopes": []},
        {"scopes": ["ALL", "INBOX"]},
        {"scopes": [42]},
        {"scopes": "ALL"},
    ):
        async with seeded_db.sessionmaker() as session:
            state = (await session.execute(select(OnboardingState))).scalar_one()
            state.data = data
            await session.commit()
            assert await get_effective_scope(session, tenant_id=1, user_id=1) is None


async def test_get_effective_scope_with_a_revoked_connection_is_not_configured(
    seeded_db: Database, oauth_key: str
) -> None:
    """Revoke destroys the stored blob: a stale selection is NOT_CONFIGURED."""
    await _store_state(seeded_db, {"scopes": [ALL_SCOPE]})
    await _store_connection(seeded_db, oauth_key, None, status=ConnectionStatus.REVOKED)
    async with seeded_db.sessionmaker() as session:
        assert await get_effective_scope(session, tenant_id=1, user_id=1) is None


def test_the_module_manifest_declares_the_mailbox_scope_step() -> None:
    assert [(step.key, step.type) for step in GOOGLE_MAIL_MANIFEST.onboarding] == [
        ("connect_google", "redirect"),
        ("select_scope", "scope_select"),
        ("test_connection", "test_connection"),
    ]
