"""Per-user onboarding API tests: manifest validation, secret hygiene, resume.

The app under test carries a local ``ModuleCatalog`` with test-only manifests
(same pattern as the catalog/connections tests), a generated Fernet key in
settings, a fresh tester registry on ``app.state.connection_testers``, and the
shared seeded database (tenants ``acme``/``globex``).

Contract under test (T4):

- walking every declared step completes the onboarding, creates the user's
  connection as ``connected`` with encrypted credentials, and leaves the
  secret answer NOWHERE: not in ``OnboardingState.data``, not in any API
  response, not in any audit payload;
- the engine advances ``form``/``scope_select`` steps, refuses wrong step
  keys (409), extra/unknown or invalid answers (422), redirect placeholders
  (501), and ``test_connection`` steps with no registered tester (501) or a
  failing tester (409) — always leaving the state unchanged on refusal;
- onboarding is per user x module: another user (same tenant or foreign)
  never sees anyone else's state, because state is derived from the principal;
- a module with zero onboarding steps answers ``completed`` with no step;
- restart resets a completed flow to its first step and audits the event.
"""

import json
import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.crypto import decrypt_credentials, generate_credentials_key
from app.core.db import Database
from app.main import create_app
from app.modules.audit.models import AuditEvent
from app.modules.catalog.manifest import FieldSpec, ModuleManifest, StepSpec
from app.modules.catalog.registry import ModuleCatalog
from app.modules.connections.models import ConnectionStatus, UserConnection
from app.modules.onboarding.models import OnboardingState, OnboardingStatus
from app.modules.onboarding.testers import ConnectionContext

BASE = "/api/v1/onboarding"
CALENDAR = "demo_calendar"
REDIRECT = "demo_redirect"
EMPTY = "demo_notes"
INACTIVE = "demo_mail"
NUMERIC = "demo_numeric"
SCOPES = ("calendar.read", "calendar.write")
SECRET = "s3cret-Calendar-Password-ñ🔐"
URL = "https://caldav.test"

CALENDAR_FORM = "credentials"
CALENDAR_SCOPES = "scopes"
CALENDAR_VERIFY = "verify"
CALENDAR_STEPS = (CALENDAR_FORM, CALENDAR_SCOPES, CALENDAR_VERIFY)

CALENDAR_ANSWERS = {
    CALENDAR_FORM: {"provider": "caldav", "caldav_url": URL, "password": SECRET},
    CALENDAR_SCOPES: {"scopes": ["calendar.read"]},
    CALENDAR_VERIFY: {},
}


@pytest.fixture
def onboarding_catalog() -> ModuleCatalog:
    """Local catalog with test-only manifests (never the default catalog).

    ``demo_calendar`` walks form -> scope_select -> test_connection with a
    provider field, a URL field, and a password field (secret by type);
    ``demo_redirect`` is a pure redirect placeholder; ``demo_notes`` declares
    no onboarding steps at all; ``demo_mail`` is registered but never
    activated for the tenant.
    """
    catalog = ModuleCatalog()
    catalog.register(
        ModuleManifest(
            key=CALENDAR,
            name="Demo Calendar",
            description="test-only calendar module for the onboarding API",
            version="1.0.0",
            permissions=SCOPES,
            onboarding=(
                StepSpec(
                    key=CALENDAR_FORM,
                    type="form",
                    title="Connect your calendar",
                    description="Account data for the CalDAV connection",
                    fields=(
                        FieldSpec(key="provider", label="Provider", required=False),
                        FieldSpec(key="caldav_url", label="CalDAV URL", type="url"),
                        FieldSpec(key="password", label="Password", type="password"),
                    ),
                ),
                StepSpec(key=CALENDAR_SCOPES, type="scope_select", title="Pick scopes"),
                StepSpec(key=CALENDAR_VERIFY, type="test_connection", title="Verify"),
            ),
        )
    )
    catalog.register(
        ModuleManifest(
            key=REDIRECT,
            name="Demo Redirect",
            description="test-only module whose onboarding redirects to the provider",
            version="1.0.0",
            onboarding=(StepSpec(key="authorize", type="redirect", title="Authorize"),),
        )
    )
    catalog.register(
        ModuleManifest(
            key=EMPTY,
            name="Demo Notes",
            description="test-only module with no onboarding steps",
            version="1.0.0",
        )
    )
    catalog.register(
        ModuleManifest(
            key=INACTIVE,
            name="Demo Mail",
            description="test-only mail module never activated for the tenant",
            version="1.0.0",
            onboarding=(StepSpec(key="connect", type="form", fields=()),),
        )
    )
    catalog.register(
        ModuleManifest(
            key=NUMERIC,
            name="Demo Numeric",
            description="test-only module exercising select options and number bounds",
            version="1.0.0",
            onboarding=(
                StepSpec(
                    key="tune",
                    type="form",
                    fields=(
                        FieldSpec(key="mode", label="Mode", type="select", options=("a", "b")),
                        FieldSpec(key="retries", label="Retries", type="number", required=False),
                    ),
                ),
            ),
        )
    )
    return catalog


@pytest.fixture
def onboarding_key() -> str:
    """A valid Fernet key for the settings of the app under test."""
    return generate_credentials_key()


@pytest.fixture
def onboarding_settings(onboarding_key: str) -> Settings:
    """Settings with a fixed JWT secret and a valid credentials key."""
    return Settings(
        jwt_secret="test-secret-0123456789-abcdefghijklmnop",
        access_token_expire_minutes=30,
        credentials_key=onboarding_key,
    )


@pytest.fixture
def testers() -> dict:
    """Fresh per-app tester registry (production ships the global one empty)."""
    return {}


@pytest.fixture
def onboarding_app(
    seeded_db: Database,
    onboarding_settings: Settings,
    onboarding_catalog: ModuleCatalog,
    testers: dict,
) -> FastAPI:
    """App whose catalog holds the test manifests and state holds fresh testers."""
    application: FastAPI = create_app()
    application.state.db = seeded_db
    application.state.settings = onboarding_settings
    application.state.module_catalog = onboarding_catalog
    application.state.connection_testers = testers
    return application


@pytest.fixture
async def onboarding_client(onboarding_app: FastAPI) -> AsyncClient:
    """HTTP client bound to :func:`onboarding_app`."""
    transport = ASGITransport(app=onboarding_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


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


async def _viewer(client: AsyncClient) -> dict[str, str]:
    return await _headers(client, "viewer@acme.test", "viewer-pass")


async def _globex_owner(client: AsyncClient) -> dict[str, str]:
    return await _headers(client, "owner@globex.test", "other-pass", "globex")


async def _activate(client: AsyncClient, headers: dict[str, str], module_key: str) -> None:
    """Activate a catalog module for the caller's tenant (owner/admin)."""
    response = await client.post(f"/api/v1/modules/{module_key}/activate", headers=headers)
    assert response.status_code == 200, response.text


async def _submit(
    client: AsyncClient, headers: dict[str, str], module_key: str, step_key: str, answers: dict
):
    """POST one step's answers; asserts the happy 200."""
    response = await client.post(
        f"{BASE}/{module_key}/steps/{step_key}", json={"answers": answers}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response


async def _walk(client: AsyncClient, headers: dict[str, str], module_key: str = CALENDAR):
    """Submit every calendar step in order; returns the completion response."""
    for step_key, answers in CALENDAR_ANSWERS.items():
        response = await _submit(client, headers, module_key, step_key, answers)
    return response


async def _state_row(db: Database, **filters) -> OnboardingState | None:
    """Load one onboarding-state row by exact column values."""
    async with db.sessionmaker() as session:
        query = select(OnboardingState)
        for name, value in filters.items():
            query = query.where(getattr(OnboardingState, name) == value)
        return (await session.execute(query)).scalar_one_or_none()


async def _states(db: Database, **filters) -> list[OnboardingState]:
    """Load every onboarding-state row matching exact column values."""
    async with db.sessionmaker() as session:
        query = select(OnboardingState)
        for name, value in filters.items():
            query = query.where(getattr(OnboardingState, name) == value)
        return list((await session.execute(query)).scalars())


async def _connections(db: Database) -> list[UserConnection]:
    async with db.sessionmaker() as session:
        return list((await session.execute(select(UserConnection))).scalars())


def _rendered_event(event: AuditEvent) -> str:
    """Serialize the client-influenced parts of an audit row for leak checks."""
    return json.dumps(
        {
            "payload": event.payload,
            "result": event.result,
            "resource_id": event.resource_id,
            "resource_type": event.resource_type,
        },
        ensure_ascii=False,
        default=str,
    )


async def _passing(context: ConnectionContext) -> bool:
    """Tester double: always verifies the connection."""
    return True


async def _failing(context: ConnectionContext) -> bool:
    """Tester double: always refuses the connection."""
    return False


async def test_endpoints_require_authentication(onboarding_client: AsyncClient) -> None:
    """Every onboarding route rejects anonymous callers."""
    assert (await onboarding_client.get(f"{BASE}/{CALENDAR}")).status_code == 401
    assert (
        await onboarding_client.post(f"{BASE}/{CALENDAR}/steps/{CALENDAR_FORM}")
    ).status_code == 401
    assert (await onboarding_client.post(f"{BASE}/{CALENDAR}/restart")).status_code == 401


async def test_get_before_start_shows_the_first_step_definition(
    onboarding_client: AsyncClient, seeded_db: Database
) -> None:
    """An untouched flow answers not_started with the first step to render."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)

    response = await onboarding_client.get(f"{BASE}/{CALENDAR}", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["module_key"] == CALENDAR
    assert body["status"] == "not_started"
    assert body["current_step"] == CALENDAR_FORM
    assert body["data"] == {}
    step = body["step"]
    assert step["key"] == CALENDAR_FORM
    assert step["type"] == "form"
    fields = {field["key"]: field for field in step["fields"]}
    assert set(fields) == {"provider", "caldav_url", "password"}
    assert fields["password"]["secret"] is True
    assert fields["caldav_url"]["type"] == "url"
    assert fields["provider"]["required"] is False
    # A read never mutates: no state row exists yet.
    assert await _states(seeded_db) == []


async def test_happy_path_completes_and_creates_encrypted_connection(
    onboarding_client: AsyncClient,
    seeded_db: Database,
    seeded_users,
    onboarding_key: str,
    testers: dict,
    audit_log,
) -> None:
    """Every step in order: completed state, connected row, secrets nowhere."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)

    async def passing(context: ConnectionContext) -> bool:
        return True

    testers[CALENDAR] = passing

    first = await _submit(
        onboarding_client, headers, CALENDAR, CALENDAR_FORM, CALENDAR_ANSWERS[CALENDAR_FORM]
    )
    assert first.json()["status"] == "in_progress"
    assert first.json()["current_step"] == CALENDAR_SCOPES
    assert SECRET not in first.text

    second = await _submit(
        onboarding_client, headers, CALENDAR, CALENDAR_SCOPES, CALENDAR_ANSWERS[CALENDAR_SCOPES]
    )
    assert second.json()["current_step"] == CALENDAR_VERIFY
    assert second.json()["data"]["provider"] == "caldav"
    assert SECRET not in second.text

    done = await _submit(onboarding_client, headers, CALENDAR, CALENDAR_VERIFY, {})
    body = done.json()
    assert body["status"] == "completed"
    assert body["current_step"] is None
    assert body["step"] is None
    assert body["data"] == {
        "provider": "caldav",
        "caldav_url": URL,
        "scopes": ["calendar.read"],
    }
    assert SECRET not in done.text

    owner = seeded_users["owner@acme.test"]
    state = await _state_row(
        seeded_db, tenant_id=owner.tenant_id, user_id=owner.id, module_key=CALENDAR
    )
    assert state is not None
    assert state.status is OnboardingStatus.COMPLETED
    assert state.current_step is None
    assert "password" not in state.data
    assert SECRET not in json.dumps(state.data)
    # Accumulated secrets are cleared the moment the connection stores them.
    assert state.secrets_encrypted is None

    connections = await _connections(seeded_db)
    assert len(connections) == 1
    connection = connections[0]
    assert connection.status is ConnectionStatus.CONNECTED
    assert connection.provider == "caldav"
    assert connection.user_id == owner.id
    assert SECRET not in connection.credentials_encrypted
    assert json.loads(decrypt_credentials(onboarding_key, connection.credentials_encrypted)) == {
        "password": SECRET
    }
    assert connection.metadata_["caldav_url"] == URL

    started = await audit_log(seeded_db, action="onboarding.started")
    assert [event.payload for event in started] == [
        {"module_key": CALENDAR, "step_key": CALENDAR_SCOPES, "status": "in_progress"}
    ]
    completed = await audit_log(seeded_db, action="onboarding.completed")
    assert [event.payload for event in completed] == [
        {"module_key": CALENDAR, "step_key": CALENDAR_VERIFY, "status": "completed"}
    ]
    stepped = await audit_log(seeded_db, action="onboarding.step_completed")
    assert [event.payload for event in stepped] == [
        {"module_key": CALENDAR, "step_key": CALENDAR_FORM, "status": "in_progress"},
        {"module_key": CALENDAR, "step_key": CALENDAR_SCOPES, "status": "in_progress"},
        {"module_key": CALENDAR, "step_key": CALENDAR_VERIFY, "status": "completed"},
    ]
    assert all(event.actor_user_id == owner.id for event in started + completed + stepped)
    for event in started + completed + stepped:
        rendered = _rendered_event(event)
        assert SECRET not in rendered
        assert "password" not in rendered
        assert "caldav_url" not in rendered


async def test_secret_answers_never_persist_in_clear_in_state_data(
    onboarding_client: AsyncClient, seeded_db: Database, seeded_users, testers: dict
) -> None:
    """After the first secret-bearing step the row holds no plaintext secret."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)
    testers[CALENDAR] = _passing

    await _submit(
        onboarding_client, headers, CALENDAR, CALENDAR_FORM, CALENDAR_ANSWERS[CALENDAR_FORM]
    )

    owner = seeded_users["owner@acme.test"]
    state = await _state_row(
        seeded_db, tenant_id=owner.tenant_id, user_id=owner.id, module_key=CALENDAR
    )
    assert state is not None
    assert state.status is OnboardingStatus.IN_PROGRESS
    assert state.data == {"provider": "caldav", "caldav_url": URL}
    assert SECRET not in json.dumps(state.data, ensure_ascii=False)
    assert state.secrets_encrypted is not None
    assert SECRET not in state.secrets_encrypted


async def _passing(context: ConnectionContext) -> bool:
    return True


async def _failing(context: ConnectionContext) -> bool:
    return False


async def test_resume_continues_where_the_user_left_off(
    onboarding_client: AsyncClient, seeded_db: Database, seeded_users, testers: dict
) -> None:
    """A later request (fresh login) picks up at the stored current step."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)
    testers[CALENDAR] = _passing

    await _submit(
        onboarding_client, headers, CALENDAR, CALENDAR_FORM, CALENDAR_ANSWERS[CALENDAR_FORM]
    )

    # A brand-new login session (new token) resumes at the scopes step.
    fresh = await _owner(onboarding_client)
    resumed = await onboarding_client.get(f"{BASE}/{CALENDAR}", headers=fresh)
    assert resumed.json()["status"] == "in_progress"
    assert resumed.json()["current_step"] == CALENDAR_SCOPES
    assert resumed.json()["data"]["caldav_url"] == URL
    assert SECRET not in resumed.text

    await _submit(
        onboarding_client, fresh, CALENDAR, CALENDAR_SCOPES, CALENDAR_ANSWERS[CALENDAR_SCOPES]
    )
    done = await _submit(onboarding_client, fresh, CALENDAR, CALENDAR_VERIFY, {})
    assert done.json()["status"] == "completed"

    owner = seeded_users["owner@acme.test"]
    assert len(await _states(seeded_db, tenant_id=owner.tenant_id)) == 1
    assert len(await _connections(seeded_db)) == 1


async def test_wrong_step_key_conflicts(
    onboarding_client: AsyncClient, seeded_db: Database, testers: dict
) -> None:
    """Submitting a step that is not the current one is 409 and writes nothing."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)
    testers[CALENDAR] = _passing

    response = await onboarding_client.post(
        f"{BASE}/{CALENDAR}/steps/{CALENDAR_VERIFY}", json={"answers": {}}, headers=headers
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "step is not the current onboarding step"
    assert await _states(seeded_db) == []


async def test_extra_and_invalid_answers_are_rejected(
    onboarding_client: AsyncClient, seeded_db: Database, testers: dict
) -> None:
    """Unknown fields, missing required values, and bad types are all 422."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)
    testers[CALENDAR] = _passing
    url = f"{BASE}/{CALENDAR}/steps/{CALENDAR_FORM}"

    extra = await onboarding_client.post(
        url,
        json={"answers": {"caldav_url": URL, "password": SECRET, "nope": "x"}},
        headers=headers,
    )
    missing = await onboarding_client.post(
        url, json={"answers": {"password": SECRET}}, headers=headers
    )
    bad_url = await onboarding_client.post(
        url,
        json={"answers": {"caldav_url": "ftp://nope", "password": SECRET}},
        headers=headers,
    )
    bad_provider = await onboarding_client.post(
        url,
        json={"answers": {"provider": "Not Valid", "caldav_url": URL, "password": SECRET}},
        headers=headers,
    )

    assert extra.status_code == 422
    assert missing.status_code == 422
    assert bad_url.status_code == 422
    assert bad_provider.status_code == 422
    assert SECRET not in extra.text
    assert await _states(seeded_db) == []
    assert await _connections(seeded_db) == []


async def test_body_with_unknown_keys_is_rejected(onboarding_client: AsyncClient) -> None:
    """The submit body itself is ``extra="forbid"``."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)

    response = await onboarding_client.post(
        f"{BASE}/{CALENDAR}/steps/{CALENDAR_FORM}",
        json={"answers": {}, "bypass": True},
        headers=headers,
    )

    assert response.status_code == 422


async def test_scope_select_validates_against_manifest_permissions(
    onboarding_client: AsyncClient, seeded_db: Database, testers: dict
) -> None:
    """Scopes outside the module's declared permissions are 422."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)
    testers[CALENDAR] = _passing
    await _submit(
        onboarding_client, headers, CALENDAR, CALENDAR_FORM, CALENDAR_ANSWERS[CALENDAR_FORM]
    )

    unknown_scope = await onboarding_client.post(
        f"{BASE}/{CALENDAR}/steps/{CALENDAR_SCOPES}",
        json={"answers": {"scopes": ["calendar.read", "mail.send"]}},
        headers=headers,
    )
    empty_scopes = await onboarding_client.post(
        f"{BASE}/{CALENDAR}/steps/{CALENDAR_SCOPES}",
        json={"answers": {"scopes": []}},
        headers=headers,
    )

    assert unknown_scope.status_code == 422
    assert empty_scopes.status_code == 422
    owner_state = await _state_row(seeded_db, module_key=CALENDAR)
    assert owner_state is not None
    assert owner_state.current_step == CALENDAR_SCOPES


async def test_test_connection_without_registered_tester_fails_closed(
    onboarding_client: AsyncClient, seeded_db: Database, audit_log
) -> None:
    """No tester for the module: 501, state unchanged, no connection, no event."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)
    # ``testers`` stays empty: production ships the registry empty too.
    await _submit(
        onboarding_client, headers, CALENDAR, CALENDAR_FORM, CALENDAR_ANSWERS[CALENDAR_FORM]
    )
    await _submit(
        onboarding_client, headers, CALENDAR, CALENDAR_SCOPES, CALENDAR_ANSWERS[CALENDAR_SCOPES]
    )

    response = await onboarding_client.post(
        f"{BASE}/{CALENDAR}/steps/{CALENDAR_VERIFY}", json={"answers": {}}, headers=headers
    )

    assert response.status_code == 501
    assert response.json()["detail"] == "no connection tester registered for this module"
    state = await _state_row(seeded_db, module_key=CALENDAR)
    assert state.status is OnboardingStatus.IN_PROGRESS
    assert state.current_step == CALENDAR_VERIFY
    assert await _connections(seeded_db) == []
    assert await audit_log(seeded_db, action="onboarding.completed") == []


async def test_failing_tester_refuses_and_leaves_state_unchanged(
    onboarding_client: AsyncClient, seeded_db: Database, testers: dict
) -> None:
    """A tester answering False is 409; the flow stays parked on the step."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)
    testers[CALENDAR] = _failing
    await _submit(
        onboarding_client, headers, CALENDAR, CALENDAR_FORM, CALENDAR_ANSWERS[CALENDAR_FORM]
    )
    await _submit(
        onboarding_client, headers, CALENDAR, CALENDAR_SCOPES, CALENDAR_ANSWERS[CALENDAR_SCOPES]
    )

    response = await onboarding_client.post(
        f"{BASE}/{CALENDAR}/steps/{CALENDAR_VERIFY}", json={"answers": {}}, headers=headers
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "connection test failed"
    state = await _state_row(seeded_db, module_key=CALENDAR)
    assert state.status is OnboardingStatus.IN_PROGRESS
    assert state.current_step == CALENDAR_VERIFY
    assert await _connections(seeded_db) == []


async def test_raising_tester_maps_to_bad_gateway(
    onboarding_client: AsyncClient, seeded_db: Database, testers: dict
) -> None:
    """A tester that raises is 502 and writes nothing."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)

    async def boom(context: ConnectionContext) -> bool:
        raise RuntimeError("upstream caldav exploded")

    testers[CALENDAR] = boom
    await _submit(
        onboarding_client, headers, CALENDAR, CALENDAR_FORM, CALENDAR_ANSWERS[CALENDAR_FORM]
    )
    await _submit(
        onboarding_client, headers, CALENDAR, CALENDAR_SCOPES, CALENDAR_ANSWERS[CALENDAR_SCOPES]
    )

    response = await onboarding_client.post(
        f"{BASE}/{CALENDAR}/steps/{CALENDAR_VERIFY}", json={"answers": {}}, headers=headers
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "connection test could not complete"
    state = await _state_row(seeded_db, module_key=CALENDAR)
    assert state.current_step == CALENDAR_VERIFY
    assert await _connections(seeded_db) == []


async def test_tester_receives_the_connection_context(
    onboarding_client: AsyncClient, seeded_users, testers: dict
) -> None:
    """The tester sees provider, secrets, and non-secret data — no more."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)
    seen: list[ConnectionContext] = []

    async def recording(context: ConnectionContext) -> bool:
        seen.append(context)
        return True

    testers[CALENDAR] = recording
    await _walk(onboarding_client, headers)

    assert len(seen) == 1
    context = seen[0]
    owner = seeded_users["owner@acme.test"]
    assert context.tenant_id == owner.tenant_id
    assert context.user_id == owner.id
    assert context.module_key == CALENDAR
    assert context.provider == "caldav"
    assert context.credentials == {"password": SECRET}
    assert context.data["scopes"] == ["calendar.read"]


async def test_redirect_step_is_a_dedicated_refusal(
    onboarding_client: AsyncClient, seeded_db: Database
) -> None:
    """Redirect placeholders never auto-complete; the refusal names the callback."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, REDIRECT)

    viewed = await onboarding_client.get(f"{BASE}/{REDIRECT}", headers=headers)
    assert viewed.status_code == 200
    assert viewed.json()["current_step"] == "authorize"
    assert viewed.json()["step"]["type"] == "redirect"

    response = await onboarding_client.post(
        f"{BASE}/{REDIRECT}/steps/authorize", json={"answers": {}}, headers=headers
    )

    assert response.status_code == 501
    assert "/api/v1/onboarding/{module_key}/callback" in response.json()["detail"]
    assert await _states(seeded_db) == []
    assert await _connections(seeded_db) == []


async def test_inactive_module_is_refused(onboarding_client: AsyncClient) -> None:
    """Registered but not activated for the tenant: 409 on read and submit."""
    headers = await _owner(onboarding_client)

    viewed = await onboarding_client.get(f"{BASE}/{INACTIVE}", headers=headers)
    submitted = await onboarding_client.post(
        f"{BASE}/{INACTIVE}/steps/connect", json={"answers": {}}, headers=headers
    )

    assert viewed.status_code == 409
    assert viewed.json()["detail"] == "module is not active for this tenant"
    assert submitted.status_code == 409


async def test_unknown_module_is_not_found(onboarding_client: AsyncClient) -> None:
    """A key outside the catalog is 404 on every route."""
    headers = await _owner(onboarding_client)

    viewed = await onboarding_client.get(f"{BASE}/ghost_module", headers=headers)
    submitted = await onboarding_client.post(
        f"{BASE}/ghost_module/steps/x", json={"answers": {}}, headers=headers
    )
    restarted = await onboarding_client.post(f"{BASE}/ghost_module/restart", headers=headers)

    assert viewed.status_code == 404
    assert viewed.json()["detail"] == "module not found"
    assert submitted.status_code == 404
    assert restarted.status_code == 404


async def test_state_is_derived_from_the_principal_only(
    onboarding_client: AsyncClient, seeded_db: Database, seeded_users, testers: dict
) -> None:
    """Another user of the tenant (and another tenant) see only their own state."""
    owner_headers = await _owner(onboarding_client)
    viewer_headers = await _viewer(onboarding_client)
    globex_headers = await _globex_owner(onboarding_client)
    await _activate(onboarding_client, owner_headers, CALENDAR)
    # Activation is per tenant: globex enables its own copy of the module.
    await _activate(onboarding_client, globex_headers, CALENDAR)
    testers[CALENDAR] = _passing
    await _submit(
        onboarding_client, owner_headers, CALENDAR, CALENDAR_FORM, CALENDAR_ANSWERS[CALENDAR_FORM]
    )

    viewer_view = await onboarding_client.get(f"{BASE}/{CALENDAR}", headers=viewer_headers)
    globex_view = await onboarding_client.get(f"{BASE}/{CALENDAR}", headers=globex_headers)

    assert viewer_view.json()["status"] == "not_started"
    assert viewer_view.json()["current_step"] == CALENDAR_FORM
    assert viewer_view.json()["data"] == {}
    assert globex_view.json()["status"] == "not_started"

    owner = seeded_users["owner@acme.test"]
    viewer = seeded_users["viewer@acme.test"]
    globex = seeded_users["owner@globex.test"]
    rows = await _states(seeded_db)
    assert len(rows) == 1
    assert rows[0].tenant_id == owner.tenant_id
    assert rows[0].user_id == owner.id
    assert viewer.id != owner.id
    assert globex.tenant_id != owner.tenant_id


async def test_module_without_steps_answers_completed(
    onboarding_client: AsyncClient,
) -> None:
    """Zero onboarding steps means nothing to configure: completed, no step."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, EMPTY)

    viewed = await onboarding_client.get(f"{BASE}/{EMPTY}", headers=headers)
    submitted = await onboarding_client.post(
        f"{BASE}/{EMPTY}/steps/anything", json={"answers": {}}, headers=headers
    )

    assert viewed.status_code == 200, viewed.text
    body = viewed.json()
    assert body["status"] == "completed"
    assert body["current_step"] is None
    assert body["step"] is None
    assert submitted.status_code == 409
    assert submitted.json()["detail"] == "step is not the current onboarding step"


async def test_provider_falls_back_to_the_module_key(
    onboarding_client: AsyncClient, seeded_db: Database, testers: dict, onboarding_key: str
) -> None:
    """No ``provider`` answer at completion: the module key becomes the provider."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)
    testers[CALENDAR] = _passing

    answers = dict(CALENDAR_ANSWERS[CALENDAR_FORM])
    answers.pop("provider")
    await _submit(onboarding_client, headers, CALENDAR, CALENDAR_FORM, answers)
    await _submit(
        onboarding_client, headers, CALENDAR, CALENDAR_SCOPES, CALENDAR_ANSWERS[CALENDAR_SCOPES]
    )
    done = await _submit(onboarding_client, headers, CALENDAR, CALENDAR_VERIFY, {})

    assert done.json()["status"] == "completed"
    connection = (await _connections(seeded_db))[0]
    assert connection.provider == CALENDAR
    assert json.loads(decrypt_credentials(onboarding_key, connection.credentials_encrypted)) == {
        "password": SECRET
    }


async def test_restart_resets_a_completed_flow(
    onboarding_client: AsyncClient, seeded_db: Database, seeded_users, testers: dict, audit_log
) -> None:
    """Restart parks the user back on the first step and audits the event."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)
    testers[CALENDAR] = _passing
    await _walk(onboarding_client, headers)

    response = await onboarding_client.post(f"{BASE}/{CALENDAR}/restart", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "in_progress"
    assert body["current_step"] == CALENDAR_FORM
    assert body["data"] == {}

    owner = seeded_users["owner@acme.test"]
    state = await _state_row(
        seeded_db, tenant_id=owner.tenant_id, user_id=owner.id, module_key=CALENDAR
    )
    assert state.status is OnboardingStatus.IN_PROGRESS
    assert state.data == {}
    assert state.secrets_encrypted is None
    # The connection row survives the restart until the user completes again.
    connection = (await _connections(seeded_db))[0]
    assert connection.status is ConnectionStatus.CONNECTED

    events = await audit_log(seeded_db, action="onboarding.restarted")
    assert [event.payload for event in events] == [
        {"module_key": CALENDAR, "step_key": CALENDAR_FORM, "status": "in_progress"}
    ]

    # The user can walk the whole flow again after a restart.
    await _walk(onboarding_client, headers)
    state_after = await _state_row(
        seeded_db, tenant_id=owner.tenant_id, user_id=owner.id, module_key=CALENDAR
    )
    assert state_after.status is OnboardingStatus.COMPLETED
    assert len(await _connections(seeded_db)) == 1


async def test_credentials_never_reach_application_logs(
    onboarding_client: AsyncClient, caplog: pytest.LogCaptureFixture, testers: dict
) -> None:
    """A full walk logs nothing that contains the plaintext secret."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)
    testers[CALENDAR] = _passing

    with caplog.at_level(logging.DEBUG):
        await _walk(onboarding_client, headers)

    assert SECRET not in caplog.text


async def test_select_options_and_number_bounds_are_enforced(
    onboarding_client: AsyncClient, seeded_db: Database
) -> None:
    """``select`` values must sit in the declared options; numbers reject bools."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, NUMERIC)
    url = f"{BASE}/{NUMERIC}/steps/tune"

    bad_select = await onboarding_client.post(url, json={"answers": {"mode": "c"}}, headers=headers)
    bool_number = await onboarding_client.post(
        url, json={"answers": {"mode": "a", "retries": True}}, headers=headers
    )

    assert bad_select.status_code == 422
    assert bool_number.status_code == 422
    assert await _states(seeded_db) == []

    done = await onboarding_client.post(
        url, json={"answers": {"mode": "b", "retries": 3}}, headers=headers
    )

    assert done.status_code == 200, done.text
    assert done.json()["status"] == "completed"
    assert done.json()["data"] == {"mode": "b", "retries": 3}
    connection = (await _connections(seeded_db))[0]
    # No provider answer anywhere: the module key is the provider.
    assert connection.provider == NUMERIC


async def test_scope_select_step_definition_exposes_the_allowlist(
    onboarding_client: AsyncClient,
) -> None:
    """The renderer sees the synthesized ``scopes`` field with its options."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)
    await _submit(
        onboarding_client, headers, CALENDAR, CALENDAR_FORM, CALENDAR_ANSWERS[CALENDAR_FORM]
    )

    response = await onboarding_client.get(f"{BASE}/{CALENDAR}", headers=headers)

    step = response.json()["step"]
    assert step["type"] == "scope_select"
    assert step["fields"] == [
        {
            "key": "scopes",
            "label": "Scopes",
            "type": "select",
            "required": True,
            "secret": False,
            "help_text": "",
            "options": list(SCOPES),
        }
    ]


async def test_restart_of_a_running_flow_is_a_noop(
    onboarding_client: AsyncClient, seeded_db: Database, audit_log
) -> None:
    """Restart only acts on completed flows; running ones come back unchanged."""
    headers = await _owner(onboarding_client)
    await _activate(onboarding_client, headers, CALENDAR)
    await _submit(
        onboarding_client, headers, CALENDAR, CALENDAR_FORM, CALENDAR_ANSWERS[CALENDAR_FORM]
    )

    response = await onboarding_client.post(f"{BASE}/{CALENDAR}/restart", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "in_progress"
    assert response.json()["current_step"] == CALENDAR_SCOPES
    assert response.json()["data"]["caldav_url"] == URL
    state = await _state_row(seeded_db, module_key=CALENDAR)
    assert state.status is OnboardingStatus.IN_PROGRESS
    assert await audit_log(seeded_db, action="onboarding.restarted") == []
