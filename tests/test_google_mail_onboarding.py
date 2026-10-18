"""Google Mail onboarding integration: OAuth scopes, labels, selection (G2).

Contract under test (google-mail M2, mirroring
``tests/test_calendar_onboarding.py``):

- ``GET``/``POST /api/v1/oauth/google_mail/authorize`` build Google's consent
  URL from the manifest's permissions, so it carries BOTH ``gmail.modify``
  and ``gmail.send`` (G1's manifest through the engine's existing
  ``scopes=manifest.permissions`` rule — no OAuth production change);
- the Google callback completes the ``connect_google`` redirect step and
  parks the flow on ``select_scope``; the step renders the user's REAL
  labels through the registered scope options provider: ``ALL``, ``INBOX``,
  then the user's labels (``users.labelsList`` over the stored token);
- submitting the selection (exactly one choice) persists it through the
  engine's answer machinery (``OnboardingState.data`` → connection
  ``metadata`` at completion) and the ``test_connection`` step still
  completes the flow with the stored OAuth tokens intact;
- a labels failure (no connection, dead tokens) fails closed: the step
  renders 200 with empty options and the engine's fixed actionable
  help_text, and a submit is 409 — never a 500, never a token leak;
- ``restart`` destroys the connection and clears the selection
  (:func:`get_effective_scope <app.modules.google_mail.scope.get_effective_scope>`
  answers NOT_CONFIGURED again).
"""

import json
import urllib.parse
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.crypto import decrypt_credentials, generate_credentials_key
from app.core.db import Database
from app.main import create_app
from app.modules.catalog.registry import ModuleCatalog
from app.modules.connections.models import ConnectionStatus, UserConnection
from app.modules.google_mail.manifest import (
    GMAIL_MODIFY_SCOPE,
    GMAIL_SEND_SCOPE,
    GOOGLE_MAIL_MANIFEST,
)
from app.modules.google_mail.provider import provider_registry, register_provider
from app.modules.google_mail.registry import (
    register_google_mail,
    register_google_mail_scope_options,
    register_google_mail_tester,
)
from app.modules.google_mail.scope import ALL_SCOPE, get_effective_scope
from app.modules.identity.deps import CurrentUser
from app.modules.identity.models import UserRole
from app.modules.oauth import service as oauth_service
from app.modules.onboarding.models import OnboardingState, OnboardingStatus
from app.modules.onboarding.options import scope_options_providers
from app.modules.onboarding.testers import connection_testers

BASE = "/api/v1/onboarding"
OAUTH = "/api/v1/oauth"
CALLBACK = f"{OAUTH}/google/callback"
MODULE = "google_mail"
ONBOARDING_LANDING = f"/configuracion/herramientas/{MODULE}/onboarding"
AUTH_HOST = "accounts.google.com"
JWT_SECRET = "test-secret-0123456789-abcdefghijklmnop"
GOOGLE_CLIENT_ID = "client.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "GOCSPX-test-client-secret"
ACCESS_TOKEN = "ya29.test-access-token"
REFRESH_TOKEN = "1//test-refresh-token"
LABELS = [
    {"id": "INBOX", "name": "Inbox", "type": "system"},
    {"id": "SENT", "name": "Sent", "type": "system"},
    {"id": "Label_7", "name": "Asistente", "type": "user"},
]


@pytest.fixture(autouse=True)
def registries_clean() -> Iterator[None]:
    """Every process-wide registry starts and ends empty per test."""
    provider_registry.clear()
    connection_testers.clear()
    scope_options_providers.clear()
    oauth_service.pending_states.clear()
    yield
    provider_registry.clear()
    connection_testers.clear()
    scope_options_providers.clear()
    oauth_service.pending_states.clear()


@pytest.fixture
def oauth_key() -> str:
    return generate_credentials_key()


@pytest.fixture
def mail_settings(oauth_key: str) -> Settings:
    return Settings(
        jwt_secret=JWT_SECRET,
        access_token_expire_minutes=30,
        credentials_key=oauth_key,
        google_client_id=GOOGLE_CLIENT_ID,
        google_client_secret=GOOGLE_CLIENT_SECRET,
    )


@pytest.fixture
def mail_catalog() -> ModuleCatalog:
    """Local catalog holding the real google_mail manifest (never the default)."""
    catalog = ModuleCatalog()
    register_google_mail(catalog)
    return catalog


@pytest.fixture
def mail_app(seeded_db: Database, mail_settings: Settings, mail_catalog: ModuleCatalog) -> FastAPI:
    """App with the google_mail catalog; state keeps the process-wide testers."""
    application = create_app()
    application.state.db = seeded_db
    application.state.settings = mail_settings
    application.state.module_catalog = mail_catalog
    return application


@pytest.fixture
async def mail_client(mail_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=mail_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _headers(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "owner@acme.test", "password": "owner-pass", "tenant_slug": "acme"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _activate(client: AsyncClient, headers: dict[str, str]) -> None:
    response = await client.post(f"/api/v1/modules/{MODULE}/activate", headers=headers)
    assert response.status_code == 200, response.text


def _owner_principal() -> CurrentUser:
    return CurrentUser(
        id=1,
        tenant_id=1,
        email="owner@acme.test",
        full_name="Acme Owner",
        role=UserRole.OWNER,
        is_superadmin=False,
    )


def _store_pending_for_owner() -> str:
    state = oauth_service.new_state()
    oauth_service.store_pending(
        state,
        user=_owner_principal(),
        module_key=MODULE,
        step_key="connect_google",
        pkce_verifier="test-verifier",
    )
    return state


def _fake_tokens() -> oauth_service.OAuthTokens:
    return oauth_service.OAuthTokens(
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
        expires_in=3600,
        scopes=(GMAIL_MODIFY_SCOPE, GMAIL_SEND_SCOPE),
    )


async def _callback(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> httpx.Response:
    async def fake_exchange_code(**kwargs: Any) -> oauth_service.OAuthTokens:
        return _fake_tokens()

    monkeypatch.setattr(oauth_service, "exchange_code", fake_exchange_code)
    state = _store_pending_for_owner()
    return await client.get(CALLBACK, params={"state": state, "code": "the-code"})


async def _state_row(db: Database) -> OnboardingState | None:
    async with db.sessionmaker() as session:
        return (
            await session.execute(
                select(OnboardingState).where(
                    OnboardingState.tenant_id == 1, OnboardingState.user_id == 1
                )
            )
        ).scalar_one_or_none()


async def _connection_row(db: Database) -> UserConnection | None:
    async with db.sessionmaker() as session:
        return (
            await session.execute(
                select(UserConnection).where(
                    UserConnection.tenant_id == 1, UserConnection.user_id == 1
                )
            )
        ).scalar_one_or_none()


async def _stored_credentials(db: Database, oauth_key: str) -> dict[str, Any]:
    row = await _connection_row(db)
    assert row is not None and row.credentials_encrypted is not None
    return json.loads(decrypt_credentials(oauth_key, row.credentials_encrypted))


class _FakeGmailProvider:
    """Provider double: fixed labels plus a passing profile read."""

    def __init__(self, access_token: str) -> None:
        self.access_token = access_token

    async def get_profile(self) -> dict[str, Any]:
        return {"emailAddress": "agent@acme.test"}

    async def list_labels(self) -> list[dict[str, Any]]:
        return LABELS


def _register_fakes() -> None:
    """Production-shaped wiring: real tester + real options hook + fake Google."""
    register_google_mail_tester()
    register_google_mail_scope_options()
    register_provider("google", _FakeGmailProvider)


# --- authorize: the consent URL carries both Gmail scopes --------------------


async def test_authorize_consent_url_carries_both_gmail_scopes(
    mail_client: AsyncClient,
) -> None:
    headers = await _headers(mail_client)
    await _activate(mail_client, headers)

    response = await mail_client.get(f"{OAUTH}/{MODULE}/authorize", headers=headers)

    assert response.status_code == 302
    parsed = urllib.parse.urlsplit(response.headers["location"])
    assert (parsed.hostname, parsed.path) == (AUTH_HOST, "/o/oauth2/v2/auth")
    granted = urllib.parse.parse_qs(parsed.query)["scope"][0].split()
    assert GMAIL_MODIFY_SCOPE in granted
    assert GMAIL_SEND_SCOPE in granted


async def test_post_authorize_consent_url_carries_both_gmail_scopes(
    mail_client: AsyncClient,
) -> None:
    """The SPA's fetch-then-navigate path answers the same URL as JSON."""
    headers = await _headers(mail_client)
    await _activate(mail_client, headers)

    response = await mail_client.post(f"{OAUTH}/{MODULE}/authorize", headers=headers)

    assert response.status_code == 200
    parsed = urllib.parse.urlsplit(response.json()["url"])
    granted = urllib.parse.parse_qs(parsed.query)["scope"][0].split()
    assert GMAIL_MODIFY_SCOPE in granted
    assert GMAIL_SEND_SCOPE in granted


# --- callback → dynamic scope_select → test_connection -----------------------


async def test_full_flow_persists_the_label_choice_and_completes(
    mail_client: AsyncClient,
    seeded_db: Database,
    oauth_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: real labels render, one choice persists, the flow completes."""
    _register_fakes()
    headers = await _headers(mail_client)
    await _activate(mail_client, headers)
    assert (await _callback(mail_client, monkeypatch)).status_code == 302

    # The flow parked on select_scope with the user's REAL labels.
    state_response = await mail_client.get(f"{BASE}/{MODULE}", headers=headers)
    body = state_response.json()
    assert body["current_step"] == "select_scope"
    step = body["step"]
    assert step["type"] == "scope_select"
    assert step["fields"][0]["options"] == [ALL_SCOPE, "INBOX", "Label_7"]
    assert step["fields"][0]["help_text"] == ""

    # Choosing a user label advances to test_connection and persists the answer.
    chosen = await mail_client.post(
        f"{BASE}/{MODULE}/steps/select_scope",
        json={"answers": {"scopes": ["Label_7"]}},
        headers=headers,
    )
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["current_step"] == "test_connection"
    assert chosen.json()["data"] == {"scopes": ["Label_7"]}

    # test_connection still works after the selection (stored OAuth tokens).
    verified = await mail_client.post(
        f"{BASE}/{MODULE}/steps/test_connection", json={"answers": {}}, headers=headers
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "completed"

    # The selection is readable through the G3 accessor; tokens survived.
    async with seeded_db.sessionmaker() as session:
        selection = await get_effective_scope(session, tenant_id=1, user_id=1)
    assert selection is not None
    assert selection.scope_id == "Label_7"
    stored = await _stored_credentials(seeded_db, oauth_key)
    assert stored["access_token"] == ACCESS_TOKEN
    assert stored["refresh_token"] == REFRESH_TOKEN
    row = await _connection_row(seeded_db)
    assert row is not None and row.metadata_.get("scopes") == ["Label_7"]


async def test_choosing_all_mail_completes_with_the_all_selection(
    mail_client: AsyncClient,
    seeded_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register_fakes()
    headers = await _headers(mail_client)
    await _activate(mail_client, headers)
    assert (await _callback(mail_client, monkeypatch)).status_code == 302

    chosen = await mail_client.post(
        f"{BASE}/{MODULE}/steps/select_scope",
        json={"answers": {"scopes": [ALL_SCOPE]}},
        headers=headers,
    )
    verified = await mail_client.post(
        f"{BASE}/{MODULE}/steps/test_connection", json={"answers": {}}, headers=headers
    )

    assert chosen.status_code == 200, chosen.text
    assert verified.status_code == 200, verified.text
    async with seeded_db.sessionmaker() as session:
        selection = await get_effective_scope(session, tenant_id=1, user_id=1)
    assert selection is not None
    assert (selection.scope_id, selection.scope_name) == (ALL_SCOPE, "All mail")


async def test_select_scope_rejects_oauth_scopes_and_double_choices(
    mail_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OAuth permissions are not mailbox scopes; exclusive means exactly one."""
    _register_fakes()
    headers = await _headers(mail_client)
    await _activate(mail_client, headers)
    assert (await _callback(mail_client, monkeypatch)).status_code == 302

    oauth_scope = await mail_client.post(
        f"{BASE}/{MODULE}/steps/select_scope",
        json={"answers": {"scopes": [GMAIL_MODIFY_SCOPE]}},
        headers=headers,
    )
    double = await mail_client.post(
        f"{BASE}/{MODULE}/steps/select_scope",
        json={"answers": {"scopes": [ALL_SCOPE, "INBOX"]}},
        headers=headers,
    )

    assert oauth_scope.status_code == 422
    assert double.status_code == 422


# --- labels failure fails closed ---------------------------------------------


async def test_scope_step_without_a_connection_fails_closed(
    mail_client: AsyncClient,
    seeded_db: Database,
) -> None:
    """Parked on select_scope with no stored connection: actionable, no 500."""
    register_google_mail_scope_options()
    headers = await _headers(mail_client)
    await _activate(mail_client, headers)
    async with seeded_db.sessionmaker() as session:
        session.add(
            OnboardingState(
                tenant_id=1,
                user_id=1,
                module_key=MODULE,
                status=OnboardingStatus.IN_PROGRESS,
                current_step="select_scope",
                data={},
                secrets_encrypted=None,
            )
        )
        await session.commit()

    read = await mail_client.get(f"{BASE}/{MODULE}", headers=headers)
    submit = await mail_client.post(
        f"{BASE}/{MODULE}/steps/select_scope",
        json={"answers": {"scopes": [ALL_SCOPE]}},
        headers=headers,
    )

    assert read.status_code == 200
    field = read.json()["step"]["fields"][0]
    assert field["options"] == []
    assert "reconnect" in field["help_text"]
    assert ACCESS_TOKEN not in read.text
    assert submit.status_code == 409


async def test_dead_tokens_at_the_scope_step_fail_closed(
    mail_client: AsyncClient,
    seeded_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider auth failure (revoked grant) becomes the same error state."""
    _register_fakes()
    headers = await _headers(mail_client)
    await _activate(mail_client, headers)
    assert (await _callback(mail_client, monkeypatch)).status_code == 302

    class _DeadProvider(_FakeGmailProvider):
        async def list_labels(self) -> list[dict[str, Any]]:
            from app.modules.google_mail.provider import GmailAuthenticationError

            raise GmailAuthenticationError("Gmail API answered 401")

    register_provider("google", _DeadProvider)

    read = await mail_client.get(f"{BASE}/{MODULE}", headers=headers)
    submit = await mail_client.post(
        f"{BASE}/{MODULE}/steps/select_scope",
        json={"answers": {"scopes": [ALL_SCOPE]}},
        headers=headers,
    )

    assert read.status_code == 200
    assert read.json()["step"]["fields"][0]["options"] == []
    assert ACCESS_TOKEN not in read.text
    assert submit.status_code == 409


# --- restart clears the selection (D7) ---------------------------------------


async def test_restart_clears_the_selection_and_destroys_the_connection(
    mail_client: AsyncClient,
    seeded_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register_fakes()
    headers = await _headers(mail_client)
    await _activate(mail_client, headers)
    assert (await _callback(mail_client, monkeypatch)).status_code == 302
    await mail_client.post(
        f"{BASE}/{MODULE}/steps/select_scope",
        json={"answers": {"scopes": ["Label_7"]}},
        headers=headers,
    )
    await mail_client.post(
        f"{BASE}/{MODULE}/steps/test_connection", json={"answers": {}}, headers=headers
    )

    response = await mail_client.post(f"{BASE}/{MODULE}/restart", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["current_step"] == "connect_google"
    assert response.json()["data"] == {}
    row = await _connection_row(seeded_db)
    assert row is not None and row.status is ConnectionStatus.REVOKED
    assert row.credentials_encrypted is None
    async with seeded_db.sessionmaker() as session:
        assert await get_effective_scope(session, tenant_id=1, user_id=1) is None


# --- manifest contract (G2 adds the middle step) -----------------------------


def test_the_manifest_declares_redirect_then_scope_then_test() -> None:
    assert [(step.key, step.type) for step in GOOGLE_MAIL_MANIFEST.onboarding] == [
        ("connect_google", "redirect"),
        ("select_scope", "scope_select"),
        ("test_connection", "test_connection"),
    ]
