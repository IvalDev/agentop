"""Google OAuth authorization-code + PKCE flow tests (calendar-mvp D2, T1).

Contract under test (design decisions of the T1 slice):

- ``GET /api/v1/oauth/{module_key}/authorize`` requires authentication (401),
  checks settings before the catalog (501 ``Google OAuth is not configured;
  set AA_GOOGLE_CLIENT_ID and AA_GOOGLE_CLIENT_SECRET`` precedes 404), then
  the module's redirect step and scopes (400), and answers 302 to Google's
  authorization endpoint with PKCE (``S256``), ``offline``/``consent``, and a
  one-shot state stored in the in-process pending store;
- ``GET /api/v1/oauth/google/callback`` is unauthenticated: it resolves the
  identity from the pending record (never from a bearer header), consumes the
  state exactly once (unknown / expired / reused share one fixed 400
  ``invalid or expired OAuth state``), surfaces provider ``error`` and a
  missing ``code`` as 400, token-exchange refusal as 400, transport failure
  as 502, and a missing encryption key as 500 fail-closed — then stores the
  tokens through ``connections.service.create_or_update`` (encrypted at rest,
  status ``connected``) and 302-redirects to ``/configuracion/herramientas``.

The token-exchange seam is ``app.modules.oauth.service.exchange_code`` (looked
up as a module attribute at call time), so endpoint tests never touch the
network; its unit tests drive ``httpx.MockTransport`` directly.
"""

import base64
import hashlib
import json
import time
import urllib.parse
from collections.abc import AsyncIterator
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
from app.modules.calendar.manifest import CALENDAR_EVENTS_SCOPE
from app.modules.calendar.registry import register_calendar
from app.modules.catalog.manifest import ModuleManifest, StepSpec
from app.modules.catalog.registry import ModuleCatalog
from app.modules.connections.models import UserConnection
from app.modules.identity.deps import CurrentUser
from app.modules.identity.models import UserRole
from app.modules.oauth import service

BASE = "/api/v1/oauth"
CALLBACK = f"{BASE}/google/callback"
AUTH_HOST = "accounts.google.com"
AUTH_PATH = "/o/oauth2/v2/auth"
WELCOME_PATH = "/configuracion/herramientas"
JWT_SECRET = "test-secret-0123456789-abcdefghijklmnop"
GOOGLE_CLIENT_ID = "client.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "GOCSPX-test-client-secret"


def _expected_challenge(verifier: str) -> str:
    """PKCE S256 challenge of ``verifier`` (base64url, no padding)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@pytest.fixture(autouse=True)
def pending_store() -> AsyncIterator[None]:
    """The process-wide pending store starts and ends empty per test."""
    service.pending_states.clear()
    yield
    service.pending_states.clear()


@pytest.fixture
def oauth_key() -> str:
    """A valid Fernet key for the app under test."""
    return generate_credentials_key()


@pytest.fixture
def google_settings(oauth_key: str) -> Settings:
    """Settings with Google OAuth client credentials (constructor beats env)."""
    return Settings(
        jwt_secret=JWT_SECRET,
        access_token_expire_minutes=30,
        credentials_key=oauth_key,
        google_client_id=GOOGLE_CLIENT_ID,
        google_client_secret=GOOGLE_CLIENT_SECRET,
    )


@pytest.fixture
def no_google_settings(oauth_key: str, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings without Google client credentials (env cleared)."""
    monkeypatch.delenv("AA_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("AA_GOOGLE_CLIENT_SECRET", raising=False)
    return Settings(
        jwt_secret=JWT_SECRET,
        access_token_expire_minutes=30,
        credentials_key=oauth_key,
    )


@pytest.fixture
def no_key_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings with Google credentials but no encryption key (fail closed)."""
    monkeypatch.delenv("AA_CREDENTIALS_KEY", raising=False)
    return Settings(
        jwt_secret=JWT_SECRET,
        access_token_expire_minutes=30,
        credentials_key=None,
        google_client_id=GOOGLE_CLIENT_ID,
        google_client_secret=GOOGLE_CLIENT_SECRET,
    )


@pytest.fixture
def oauth_catalog() -> ModuleCatalog:
    """Local catalog: the real calendar manifest plus two check-only modules."""
    catalog = ModuleCatalog()
    register_calendar(catalog)
    catalog.register(
        ModuleManifest(
            key="form_only",
            name="Form Only",
            description="module without a redirect step",
            version="0.1.0",
            permissions=(CALENDAR_EVENTS_SCOPE,),
            onboarding=(StepSpec(key="fill", type="form", title="Fill"),),
        )
    )
    catalog.register(
        ModuleManifest(
            key="no_scopes",
            name="No Scopes",
            description="module with a redirect step but no scopes",
            version="0.1.0",
            permissions=(),
            onboarding=(StepSpec(key="connect", type="redirect", title="Connect"),),
        )
    )
    return catalog


def _build_app(db: Database, settings: Settings, catalog: ModuleCatalog) -> FastAPI:
    application = create_app()
    application.state.db = db
    application.state.settings = settings
    application.state.module_catalog = catalog
    return application


@pytest.fixture
def oauth_app(
    seeded_db: Database, google_settings: Settings, oauth_catalog: ModuleCatalog
) -> FastAPI:
    """App with Google credentials, a Fernet key, and the calendar catalog."""
    return _build_app(seeded_db, google_settings, oauth_catalog)


@pytest.fixture
async def oauth_client(oauth_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=oauth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def no_google_app(
    seeded_db: Database, no_google_settings: Settings, oauth_catalog: ModuleCatalog
) -> FastAPI:
    """App whose settings lack the Google client credentials."""
    return _build_app(seeded_db, no_google_settings, oauth_catalog)


@pytest.fixture
async def no_google_client(no_google_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=no_google_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def no_key_app(
    seeded_db: Database, no_key_settings: Settings, oauth_catalog: ModuleCatalog
) -> FastAPI:
    """App with Google credentials but no credentials encryption key."""
    return _build_app(seeded_db, no_key_settings, oauth_catalog)


@pytest.fixture
async def no_key_client(no_key_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=no_key_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _headers(
    client: AsyncClient, email: str, password: str, tenant_slug: str = "acme"
) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "tenant_slug": tenant_slug},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _owner(client: AsyncClient) -> dict[str, str]:
    return await _headers(client, "owner@acme.test", "owner-pass")


async def _authorize(
    client: AsyncClient, module_key: str = "calendar", headers: dict[str, str] | None = None
) -> httpx.Response:
    return await client.get(f"{BASE}/{module_key}/authorize", headers=headers or {})


def _owner_principal() -> CurrentUser:
    """The acme owner as ``get_current_user`` would resolve them."""
    return CurrentUser(
        id=1,
        tenant_id=1,
        email="owner@acme.test",
        full_name="Acme Owner",
        role=UserRole.OWNER,
        is_superadmin=False,
    )


def _store_pending_for_owner(
    *,
    module_key: str = "calendar",
    step_key: str = "connect_google",
    verifier: str = "test-verifier",
) -> str:
    """Seed the pending store as the start endpoint would for the acme owner."""
    state = service.new_state()
    service.store_pending(
        state,
        user=_owner_principal(),
        module_key=module_key,
        step_key=step_key,
        pkce_verifier=verifier,
    )
    return state


def _fake_tokens(**overrides: Any) -> service.OAuthTokens:
    values: dict[str, Any] = {
        "access_token": "ya29.test-access-token",
        "refresh_token": "1//test-refresh-token",
        "expires_in": 3600,
        "scopes": (CALENDAR_EVENTS_SCOPE,),
    }
    values.update(overrides)
    return service.OAuthTokens(**values)


# --- start endpoint ----------------------------------------------------------


async def test_authorize_requires_authentication(oauth_client: AsyncClient) -> None:
    response = await _authorize(oauth_client)
    assert response.status_code == 401


async def test_authorize_without_google_credentials_returns_501(
    no_google_client: AsyncClient,
) -> None:
    headers = await _owner(no_google_client)
    response = await _authorize(no_google_client, headers=headers)
    assert response.status_code == 501
    assert response.json()["detail"] == (
        "Google OAuth is not configured; set AA_GOOGLE_CLIENT_ID and AA_GOOGLE_CLIENT_SECRET"
    )


async def test_authorize_settings_check_precedes_module_lookup(
    no_google_client: AsyncClient,
) -> None:
    """Unconfigured settings answer 501 even for a module that does not exist."""
    headers = await _owner(no_google_client)
    response = await _authorize(no_google_client, "missing_module", headers=headers)
    assert response.status_code == 501


async def test_authorize_unknown_module_returns_404(oauth_client: AsyncClient) -> None:
    headers = await _owner(oauth_client)
    response = await _authorize(oauth_client, "missing_module", headers=headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "module not found"


async def test_authorize_module_without_redirect_step_returns_400(
    oauth_client: AsyncClient,
) -> None:
    headers = await _owner(oauth_client)
    response = await _authorize(oauth_client, "form_only", headers=headers)
    assert response.status_code == 400


async def test_authorize_module_without_scopes_returns_400(
    oauth_client: AsyncClient,
) -> None:
    headers = await _owner(oauth_client)
    response = await _authorize(oauth_client, "no_scopes", headers=headers)
    assert response.status_code == 400


async def test_authorize_redirects_to_google_with_pkce_and_state(
    oauth_client: AsyncClient,
) -> None:
    headers = await _owner(oauth_client)
    response = await _authorize(oauth_client, headers=headers)

    assert response.status_code == 302
    parsed = urllib.parse.urlsplit(response.headers["location"])
    assert (parsed.hostname, parsed.path) == (AUTH_HOST, AUTH_PATH)
    query = urllib.parse.parse_qs(parsed.query)
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["client_id"] == [GOOGLE_CLIENT_ID]
    assert query["redirect_uri"] == [f"http://test{CALLBACK}"]
    assert CALENDAR_EVENTS_SCOPE in query["scope"][0].split()

    state = query["state"][0]
    pending = service.pending_states[state]
    assert pending.user.id == 1  # the logged-in acme owner resolved server-side
    assert pending.module_key == "calendar"
    assert pending.step_key == "connect_google"
    assert query["code_challenge"] == [_expected_challenge(pending.pkce_verifier)]


# --- callback: state lifecycle ----------------------------------------------


async def test_callback_unknown_state_returns_400(oauth_client: AsyncClient) -> None:
    response = await oauth_client.get(CALLBACK, params={"state": "bogus", "code": "x"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid or expired OAuth state"


async def test_callback_missing_state_returns_400(oauth_client: AsyncClient) -> None:
    response = await oauth_client.get(CALLBACK, params={"code": "x"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid or expired OAuth state"


async def test_callback_reused_state_returns_400(
    oauth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_exchange_code(**kwargs: Any) -> service.OAuthTokens:
        return _fake_tokens()

    monkeypatch.setattr(service, "exchange_code", fake_exchange_code)
    state = _store_pending_for_owner()

    first = await oauth_client.get(CALLBACK, params={"state": state, "code": "once"})
    second = await oauth_client.get(CALLBACK, params={"state": state, "code": "again"})

    assert first.status_code == 302
    assert second.status_code == 400
    assert second.json()["detail"] == "invalid or expired OAuth state"


async def test_callback_expired_state_returns_400(
    oauth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(service, "_now", lambda: clock["now"])
    state = _store_pending_for_owner()
    clock["now"] += service.PENDING_TTL_SECONDS + 1

    response = await oauth_client.get(CALLBACK, params={"state": state, "code": "x"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid or expired OAuth state"
    assert state not in service.pending_states


async def test_callback_provider_error_returns_400(oauth_client: AsyncClient) -> None:
    state = _store_pending_for_owner()
    response = await oauth_client.get(CALLBACK, params={"state": state, "error": "access_denied"})
    assert response.status_code == 400
    assert response.json()["detail"] == "authorization failed"


async def test_callback_missing_code_returns_400(oauth_client: AsyncClient) -> None:
    state = _store_pending_for_owner()
    response = await oauth_client.get(CALLBACK, params={"state": state})
    assert response.status_code == 400
    assert response.json()["detail"] == "missing authorization code"


# --- callback: success path --------------------------------------------------


async def test_callback_stores_encrypted_connection_and_redirects(
    oauth_client: AsyncClient,
    oauth_key: str,
    seeded_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exchange_code(**kwargs: Any) -> service.OAuthTokens:
        return _fake_tokens()

    monkeypatch.setattr(service, "exchange_code", fake_exchange_code)
    state = _store_pending_for_owner()

    response = await oauth_client.get(CALLBACK, params={"state": state, "code": "the-code"})

    assert response.status_code == 302
    assert response.headers["location"] == WELCOME_PATH
    assert service.pending_states == {}

    async with seeded_db.sessionmaker() as session:
        rows = list(
            (
                await session.execute(
                    select(UserConnection).where(
                        UserConnection.module_key == "calendar",
                        UserConnection.provider == "google",
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1
    row = rows[0]
    assert (row.tenant_id, row.user_id) == (1, 1)
    assert row.status.value == "connected"

    plaintext = decrypt_credentials(oauth_key, row.credentials_encrypted)
    stored = json.loads(plaintext)
    assert stored["access_token"] == "ya29.test-access-token"
    assert stored["refresh_token"] == "1//test-refresh-token"
    assert isinstance(stored["expires_at"], int)
    assert stored["expires_at"] >= int(time.time()) + 3000
    assert stored["scopes"] == [CALENDAR_EVENTS_SCOPE]
    assert GOOGLE_CLIENT_SECRET not in plaintext


async def test_callback_passes_pkce_verifier_to_token_exchange(
    oauth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_exchange_code(**kwargs: Any) -> service.OAuthTokens:
        captured.update(kwargs)
        return _fake_tokens()

    monkeypatch.setattr(service, "exchange_code", fake_exchange_code)
    state = _store_pending_for_owner(verifier="stored-verifier-abc")

    response = await oauth_client.get(CALLBACK, params={"state": state, "code": "the-code"})

    assert response.status_code == 302
    assert captured["code"] == "the-code"
    assert captured["code_verifier"] == "stored-verifier-abc"
    assert captured["redirect_uri"] == f"http://test{CALLBACK}"
    assert captured["client_id"] == GOOGLE_CLIENT_ID
    assert captured["client_secret"] == GOOGLE_CLIENT_SECRET


# --- callback: exchange and storage failures --------------------------------


async def test_callback_token_exchange_refusal_returns_400(
    oauth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refused_exchange_code(**kwargs: Any) -> service.OAuthTokens:
        raise service.TokenExchangeError("authorization code rejected")

    monkeypatch.setattr(service, "exchange_code", refused_exchange_code)
    state = _store_pending_for_owner()

    response = await oauth_client.get(CALLBACK, params={"state": state, "code": "bad"})
    assert response.status_code == 400
    assert response.json()["detail"] == "token exchange failed"
    assert service.pending_states == {}


async def test_callback_transport_failure_returns_502(
    oauth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreachable_exchange_code(**kwargs: Any) -> service.OAuthTokens:
        raise httpx.RequestError(
            "connection failed",
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        )

    monkeypatch.setattr(service, "exchange_code", unreachable_exchange_code)
    state = _store_pending_for_owner()

    response = await oauth_client.get(CALLBACK, params={"state": state, "code": "x"})
    assert response.status_code == 502
    assert service.pending_states == {}


async def test_callback_without_credentials_key_fails_closed(
    no_key_client: AsyncClient, seeded_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_exchange_code(**kwargs: Any) -> service.OAuthTokens:
        return _fake_tokens()

    monkeypatch.setattr(service, "exchange_code", fake_exchange_code)
    state = _store_pending_for_owner()

    response = await no_key_client.get(CALLBACK, params={"state": state, "code": "x"})

    assert response.status_code == 500
    assert response.json()["detail"] == "credentials encryption is not configured"
    async with seeded_db.sessionmaker() as session:
        count = len(list((await session.execute(select(UserConnection))).scalars()))
    assert count == 0


# --- service unit tests ------------------------------------------------------


def test_pending_state_is_single_use_and_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 5_000.0}
    monkeypatch.setattr(service, "_now", lambda: clock["now"])
    state = service.new_state()
    service.store_pending(
        state,
        user=_owner_principal(),
        module_key="calendar",
        step_key="connect_google",
        pkce_verifier="v",
    )

    consumed = service.consume_pending(state)
    assert consumed is not None
    assert consumed.pkce_verifier == "v"
    assert service.consume_pending(state) is None
    assert service.consume_pending("never-stored") is None

    clock["now"] = 5_000.0
    expired_state = service.new_state()
    service.store_pending(
        expired_state,
        user=_owner_principal(),
        module_key="calendar",
        step_key="connect_google",
        pkce_verifier="v",
    )
    clock["now"] += service.PENDING_TTL_SECONDS + 1
    assert service.consume_pending(expired_state) is None
    assert expired_state not in service.pending_states


async def test_exchange_code_posts_pkce_form_and_parses_tokens() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = dict(urllib.parse.parse_qsl(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.from-google",
                "refresh_token": "rt-1",
                "expires_in": 3599,
                "scope": CALENDAR_EVENTS_SCOPE,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tokens = await service.exchange_code(
            code="auth-code",
            redirect_uri="http://test/api/v1/oauth/google/callback",
            client_id="cid",
            client_secret="csecret",
            code_verifier="pkce-verifier",
            client=client,
        )

    assert seen["url"] == "https://oauth2.googleapis.com/token"
    assert seen["form"] == {
        "code": "auth-code",
        "client_id": "cid",
        "client_secret": "csecret",
        "redirect_uri": "http://test/api/v1/oauth/google/callback",
        "grant_type": "authorization_code",
        "code_verifier": "pkce-verifier",
    }
    assert tokens.access_token == "ya29.from-google"
    assert tokens.refresh_token == "rt-1"
    assert tokens.expires_in == 3599
    assert tokens.scopes == (CALENDAR_EVENTS_SCOPE,)


async def test_exchange_code_refusal_raises_token_exchange_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(service.TokenExchangeError):
            await service.exchange_code(
                code="bad",
                redirect_uri="http://test/cb",
                client_id="cid",
                client_secret="csecret",
                code_verifier="v",
                client=client,
            )


async def test_exchange_code_missing_token_raises_token_exchange_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(service.TokenExchangeError):
            await service.exchange_code(
                code="x",
                redirect_uri="http://test/cb",
                client_id="cid",
                client_secret="csecret",
                code_verifier="v",
                client=client,
            )


async def test_exchange_code_transport_error_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.RequestError):
            await service.exchange_code(
                code="x",
                redirect_uri="http://test/cb",
                client_id="cid",
                client_secret="csecret",
                code_verifier="v",
                client=client,
            )
