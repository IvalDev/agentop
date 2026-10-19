"""Calendar onboarding integration (calendar-mvp T2): callback → wizard → test.

Contract under test (T2 / D7 / D8):

- the Google OAuth callback, after storing the connection, completes the
  module's ``redirect`` step through
  :func:`complete_redirect_step <app.modules.onboarding.service.complete_redirect_step>`
  and lands the browser on ``/configuracion/herramientas/{module_key}/onboarding``
  (the D8 generic wizard route — the web app does not serve it until T4, a
  harmless 404 until then);
- that completion is best-effort: an inactive or already-completed onboarding
  flow is logged server-side and the redirect still happens — the connection
  IS stored either way, and the browser flow never breaks;
- the ``test_connection`` step bridges the stored ``UserConnection``
  credentials into ``ConnectionContext``: the calendar tester sees the OAuth
  access token the callback stored (provider ``google``), and a passing test
  completes the flow WITHOUT destroying the stored tokens;
- the real registration entry points (:func:`register_calendar_tester` +
  :func:`register_google_provider`) wire the process-wide registries when a
  caller asks — production stays dormant (the T1 allowlist decision);
- ``restart`` on a completed calendar flow destroys the connection
  credentials (status ``revoked``, blob gone — D7) and parks the flow back at
  ``connect_google``; it is safe when no connection exists;
- POST-submitting the ``redirect`` step still answers 501: the generic engine
  contract is intact, only the OAuth callback completes a redirect step;
- a ``test_connection`` step with no stored connection fails per the existing
  tester contract (the real tester answers False without a token → 409),
  never a crash.
"""

import json
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
from app.modules.calendar.manifest import CALENDAR_EVENTS_SCOPE
from app.modules.calendar.provider import provider_registry, register_provider
from app.modules.calendar.registry import register_calendar, register_calendar_tester
from app.modules.catalog.exceptions import UnknownModuleError
from app.modules.catalog.registry import ModuleCatalog
from app.modules.connections.models import ConnectionStatus, UserConnection
from app.modules.identity.deps import CurrentUser
from app.modules.identity.models import UserRole
from app.modules.oauth import service as oauth_service
from app.modules.onboarding import service as onboarding_service
from app.modules.onboarding.exceptions import ModuleInactiveError
from app.modules.onboarding.models import OnboardingState, OnboardingStatus
from app.modules.onboarding.testers import ConnectionContext, connection_testers

BASE = "/api/v1/onboarding"
CALLBACK = "/api/v1/oauth/google/callback"
MODULE = "calendar"
ONBOARDING_LANDING = f"/configuracion/herramientas/{MODULE}/onboarding"
JWT_SECRET = "test-secret-0123456789-abcdefghijklmnop"
GOOGLE_CLIENT_ID = "client.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "GOCSPX-test-client-secret"
ACCESS_TOKEN = "ya29.test-access-token"
REFRESH_TOKEN = "1//test-refresh-token"


@pytest.fixture(autouse=True)
def registries_clean() -> Iterator[None]:
    """Both process-wide registries start and end empty per test.

    The app under test uses the production shape (``app.state`` holds the
    process-wide registries), so tests wire them through the real entry
    points — e.g. ``register_calendar_tester()`` — and this fixture rolls
    them back, keeping the production registries empty between tests.
    """
    provider_registry.clear()
    connection_testers.clear()
    oauth_service.pending_states.clear()
    yield
    provider_registry.clear()
    connection_testers.clear()
    oauth_service.pending_states.clear()


@pytest.fixture
def oauth_key() -> str:
    return generate_credentials_key()


@pytest.fixture
def calendar_settings(oauth_key: str) -> Settings:
    """Settings with Google OAuth client credentials and a Fernet key."""
    return Settings(
        jwt_secret=JWT_SECRET,
        access_token_expire_minutes=30,
        credentials_key=oauth_key,
        google_client_id=GOOGLE_CLIENT_ID,
        google_client_secret=GOOGLE_CLIENT_SECRET,
    )


@pytest.fixture
def calendar_catalog() -> ModuleCatalog:
    """Local catalog holding the real calendar manifest (never the default)."""
    catalog = ModuleCatalog()
    register_calendar(catalog)
    return catalog


@pytest.fixture
def calendar_app(
    seeded_db: Database, calendar_settings: Settings, calendar_catalog: ModuleCatalog
) -> FastAPI:
    """App with the calendar catalog; state keeps the process-wide testers."""
    application = create_app()
    application.state.db = seeded_db
    application.state.settings = calendar_settings
    application.state.module_catalog = calendar_catalog
    return application


@pytest.fixture
async def calendar_client(calendar_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=calendar_app)
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
    """The acme owner as ``get_current_user`` would resolve them."""
    return CurrentUser(
        id=1,
        tenant_id=1,
        email="owner@acme.test",
        full_name="Acme Owner",
        role=UserRole.OWNER,
        is_superadmin=False,
    )


def _store_pending_for_owner() -> str:
    """Seed the pending store as the authorize endpoint would."""
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
        scopes=(CALENDAR_EVENTS_SCOPE,),
    )


async def _callback(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> httpx.Response:
    """Run a successful Google callback for the acme owner (fake exchange)."""

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


async def _complete_test_step(client: AsyncClient, headers: dict[str, str], tester) -> None:
    """Register ``tester`` for calendar and submit the test_connection step."""
    connection_testers[MODULE] = tester
    response = await client.post(
        f"{BASE}/{MODULE}/steps/test_connection", json={"answers": {}}, headers=headers
    )
    assert response.status_code == 200, response.text


class _FakeGoogleProvider:
    """Provider double: records the token it was built with, lists nothing."""

    def __init__(self, access_token: str) -> None:
        self.access_token = access_token

    async def list_events(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


# --- callback completes the redirect step (best-effort) ----------------------


async def test_callback_completes_redirect_step_and_lands_on_onboarding_wizard(
    calendar_client: AsyncClient,
    seeded_db: Database,
    seeded_users,
    audit_log,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: connection stored, redirect step advanced, D8 landing."""
    headers = await _headers(calendar_client)
    await _activate(calendar_client, headers)

    response = await _callback(calendar_client, monkeypatch)

    assert response.status_code == 302
    assert response.headers["location"] == ONBOARDING_LANDING
    assert oauth_service.pending_states == {}

    # The onboarding flow advanced from connect_google to test_connection.
    owner = seeded_users["owner@acme.test"]
    state = await _state_row(seeded_db)
    assert state is not None
    assert (state.tenant_id, state.user_id) == (owner.tenant_id, owner.id)
    assert state.status is OnboardingStatus.IN_PROGRESS
    assert state.current_step == "test_connection"
    assert state.data == {}
    assert state.secrets_encrypted is None

    # The connection was stored by the callback itself (T1 contract intact).
    row = await _connection_row(seeded_db)
    assert row is not None
    assert row.status is ConnectionStatus.CONNECTED
    assert row.provider == "google"

    # The same state-change audit events a submitted step would emit.
    stepped = await audit_log(seeded_db, action="onboarding.step_completed")
    assert [event.payload for event in stepped] == [
        {"module_key": MODULE, "step_key": "connect_google", "status": "in_progress"}
    ]
    started = await audit_log(seeded_db, action="onboarding.started")
    assert [event.payload for event in started] == [
        {"module_key": MODULE, "step_key": "test_connection", "status": "in_progress"}
    ]
    assert all(event.actor_user_id == owner.id for event in stepped + started)


async def test_callback_is_best_effort_when_module_is_inactive(
    calendar_client: AsyncClient,
    seeded_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No activation: the connection is stored, the redirect still happens.

    The module is in the catalog (so ``create_or_update`` accepts it) but the
    tenant never activated it — ``complete_redirect_step`` refuses, the
    callback logs and lands on the same wizard path anyway.
    """
    # No ``_activate`` call on purpose.

    response = await _callback(calendar_client, monkeypatch)

    assert response.status_code == 302
    assert response.headers["location"] == ONBOARDING_LANDING
    assert await _state_row(seeded_db) is None
    row = await _connection_row(seeded_db)
    assert row is not None
    assert row.status is ConnectionStatus.CONNECTED


async def test_callback_after_completed_onboarding_still_redirects(
    calendar_client: AsyncClient,
    seeded_db: Database,
    audit_log,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second callback on a completed flow redirects and changes nothing."""
    headers = await _headers(calendar_client)
    await _activate(calendar_client, headers)
    assert (await _callback(calendar_client, monkeypatch)).status_code == 302

    async def passing(context: ConnectionContext) -> bool:
        return True

    await _complete_test_step(calendar_client, headers, passing)
    stepped_before = [
        event.payload for event in await audit_log(seeded_db, action="onboarding.step_completed")
    ]
    assert len(stepped_before) == 2  # connect_google + test_connection

    second = await _callback(calendar_client, monkeypatch)

    assert second.status_code == 302
    assert second.headers["location"] == ONBOARDING_LANDING
    state = await _state_row(seeded_db)
    assert state is not None and state.status is OnboardingStatus.COMPLETED
    stepped_after = [
        event.payload for event in await audit_log(seeded_db, action="onboarding.step_completed")
    ]
    assert stepped_after == stepped_before


# --- test_connection: the UserConnection bridge ------------------------------


async def test_test_connection_bridges_stored_connection_credentials(
    calendar_client: AsyncClient,
    seeded_db: Database,
    oauth_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tester receives the stored OAuth tokens; completing keeps them."""
    headers = await _headers(calendar_client)
    await _activate(calendar_client, headers)
    assert (await _callback(calendar_client, monkeypatch)).status_code == 302
    seen: list[ConnectionContext] = []

    async def recording(context: ConnectionContext) -> bool:
        seen.append(context)
        return True

    await _complete_test_step(calendar_client, headers, recording)

    assert len(seen) == 1
    context = seen[0]
    assert context.module_key == MODULE
    assert context.provider == "google"
    assert context.credentials["access_token"] == ACCESS_TOKEN
    assert context.credentials["refresh_token"] == REFRESH_TOKEN
    assert isinstance(context.credentials["expires_at"], int)
    assert context.credentials["scopes"] == [CALENDAR_EVENTS_SCOPE]

    # The final write must NOT destroy the OAuth tokens with an empty payload.
    stored = await _stored_credentials(seeded_db, oauth_key)
    assert stored["access_token"] == ACCESS_TOKEN
    assert stored["refresh_token"] == REFRESH_TOKEN
    assert stored["scopes"] == [CALENDAR_EVENTS_SCOPE]

    state = await _state_row(seeded_db)
    assert state is not None and state.status is OnboardingStatus.COMPLETED
    assert state.current_step is None


async def test_real_calendar_tester_registration_completes_the_flow(
    calendar_client: AsyncClient,
    seeded_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """register_calendar_tester + a fake provider drive the real tester E2E."""
    captured: dict[str, Any] = {}
    fake = _FakeGoogleProvider(ACCESS_TOKEN)

    def factory(access_token: str) -> _FakeGoogleProvider:
        captured["access_token"] = access_token
        return fake

    register_calendar_tester()
    register_provider("google", factory)

    headers = await _headers(calendar_client)
    await _activate(calendar_client, headers)
    assert (await _callback(calendar_client, monkeypatch)).status_code == 302

    response = await calendar_client.post(
        f"{BASE}/{MODULE}/steps/test_connection", json={"answers": {}}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert captured["access_token"] == ACCESS_TOKEN
    state = await _state_row(seeded_db)
    assert state is not None and state.status is OnboardingStatus.COMPLETED


async def test_test_connection_without_stored_connection_fails_closed(
    calendar_client: AsyncClient,
    seeded_db: Database,
) -> None:
    """No connection row: the real tester has no token, answers False → 409."""
    headers = await _headers(calendar_client)
    await _activate(calendar_client, headers)
    async with seeded_db.sessionmaker() as session:
        session.add(
            OnboardingState(
                tenant_id=1,
                user_id=1,
                module_key=MODULE,
                status=OnboardingStatus.IN_PROGRESS,
                current_step="test_connection",
                data={},
                secrets_encrypted=None,
            )
        )
        await session.commit()
    register_calendar_tester()

    response = await calendar_client.post(
        f"{BASE}/{MODULE}/steps/test_connection", json={"answers": {}}, headers=headers
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "connection test failed"
    state = await _state_row(seeded_db)
    assert state is not None and state.current_step == "test_connection"
    assert await _connection_row(seeded_db) is None


# --- restart destroys the connection (D7) ------------------------------------


async def test_restart_destroys_connection_and_parks_at_connect_google(
    calendar_client: AsyncClient,
    seeded_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    audit_log,
) -> None:
    """Restart: credentials destroyed (revoked), flow back at connect_google."""
    headers = await _headers(calendar_client)
    await _activate(calendar_client, headers)
    assert (await _callback(calendar_client, monkeypatch)).status_code == 302

    async def passing(context: ConnectionContext) -> bool:
        return True

    await _complete_test_step(calendar_client, headers, passing)

    response = await calendar_client.post(f"{BASE}/{MODULE}/restart", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "in_progress"
    assert body["current_step"] == "connect_google"
    assert body["data"] == {}

    row = await _connection_row(seeded_db)
    assert row is not None
    assert row.status is ConnectionStatus.REVOKED
    assert row.credentials_encrypted is None

    state = await _state_row(seeded_db)
    assert state is not None and state.status is OnboardingStatus.IN_PROGRESS
    assert state.current_step == "connect_google"

    restarted = await audit_log(seeded_db, action="onboarding.restarted")
    assert [event.payload for event in restarted] == [
        {"module_key": MODULE, "step_key": "connect_google", "status": "in_progress"}
    ]
    revoked = await audit_log(seeded_db, action="connection.revoked")
    assert [event.payload for event in revoked] == [
        {"module_key": MODULE, "provider": "google", "status": "revoked"}
    ]

    # The flow is repeatable: a fresh callback reconnects the same row.
    second = await _callback(calendar_client, monkeypatch)
    assert second.status_code == 302
    row = await _connection_row(seeded_db)
    assert row is not None and row.status is ConnectionStatus.CONNECTED
    assert row.credentials_encrypted is not None


async def test_restart_without_a_connection_is_safe(
    calendar_client: AsyncClient, seeded_db: Database
) -> None:
    """A completed state with no connection row restarts without crashing."""
    headers = await _headers(calendar_client)
    await _activate(calendar_client, headers)
    async with seeded_db.sessionmaker() as session:
        session.add(
            OnboardingState(
                tenant_id=1,
                user_id=1,
                module_key=MODULE,
                status=OnboardingStatus.COMPLETED,
                current_step=None,
                data={},
                secrets_encrypted=None,
            )
        )
        await session.commit()

    response = await calendar_client.post(f"{BASE}/{MODULE}/restart", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["current_step"] == "connect_google"
    assert await _connection_row(seeded_db) is None


# --- generic engine contract stays intact ------------------------------------


async def test_post_to_redirect_step_still_returns_501(
    calendar_client: AsyncClient, seeded_db: Database
) -> None:
    """POST-submitting the redirect step is still the generic 501 refusal."""
    headers = await _headers(calendar_client)
    await _activate(calendar_client, headers)

    response = await calendar_client.post(
        f"{BASE}/{MODULE}/steps/connect_google", json={"answers": {}}, headers=headers
    )

    assert response.status_code == 501
    assert "/api/v1/onboarding/{module_key}/callback" in response.json()["detail"]
    assert await _state_row(seeded_db) is None
    assert await _connection_row(seeded_db) is None


# --- service-level: complete_redirect_step -----------------------------------


async def test_complete_redirect_step_service_requires_the_redirect_step(
    calendar_app: FastAPI, seeded_db: Database, seeded_users
) -> None:
    """The service refuses unknown, inactive, and non-redirect states."""
    owner = seeded_users["owner@acme.test"]
    catalog = calendar_app.state.module_catalog
    async with seeded_db.sessionmaker() as session:
        with pytest.raises(UnknownModuleError):
            await onboarding_service.complete_redirect_step(
                session, catalog=catalog, credentials_key=None, caller=owner, module_key="ghost"
            )
        with pytest.raises(ModuleInactiveError):
            await onboarding_service.complete_redirect_step(
                session, catalog=catalog, credentials_key=None, caller=owner, module_key=MODULE
            )
