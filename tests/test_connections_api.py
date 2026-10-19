"""Per-user connections API tests: secret hygiene, isolation, RBAC, and audit.

The app under test carries a local ``ModuleCatalog`` with test-only manifests
(same pattern as the catalog tests), a generated Fernet key in settings, and
the shared seeded database (tenants ``acme``/``globex``).

Contract under test (T3):

- ``POST`` accepts write-only ``credentials`` and every read answers without
  them: no field name, no plaintext, and no stored token in any body or log;
- owner-scoped routes see only the caller's own rows — another user of the
  same tenant and another tenant are indistinguishable 404s;
- ``GET /tenant`` and ``DELETE /{id}/revoke`` are owner/admin only;
- revoke destroys the encrypted blob; audit events carry
  ``{module_key, provider, status}`` and nothing else;
- an unknown ``module_key`` is a 404 before any row is written, and a missing
  encryption key fails closed with a clear 500 instead of storing plaintext.
"""

import json
import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.crypto import decrypt_credentials, generate_credentials_key
from app.core.db import Database
from app.main import create_app
from app.modules.audit.models import AuditEvent
from app.modules.catalog.manifest import ModuleManifest
from app.modules.catalog.registry import ModuleCatalog
from app.modules.connections.exceptions import (
    ConnectionConflictError,
    ConnectionNotFoundError,
    CredentialsEncryptionError,
)
from app.modules.connections.models import ConnectionStatus, UserConnection
from app.modules.connections.service import (
    create_or_update,
    get_for_tenant,
    replace,
    set_status,
)

BASE = "/api/v1/connections"
CALENDAR = "demo_calendar"
MAIL = "demo_mail"
PROVIDER = "caldav"
SECRET = "s3cret-Calendar-Password-ñ🔐"
ROTATED_SECRET = "rotated-password-42"
CREDS = {"endpoint": "https://caldav.test", "password": SECRET}


@pytest.fixture
def demo_catalog() -> ModuleCatalog:
    """Local catalog with two test-only manifests (never the default catalog)."""
    catalog = ModuleCatalog()
    catalog.register(
        ModuleManifest(
            key=CALENDAR,
            name="Demo Calendar",
            description="test-only calendar module for the connections API",
            version="1.0.0",
        )
    )
    catalog.register(
        ModuleManifest(
            key=MAIL,
            name="Demo Mail",
            description="test-only mail module for the connections API",
            version="0.1.0",
        )
    )
    return catalog


@pytest.fixture
def connections_key() -> str:
    """A valid Fernet key for the settings of the app under test."""
    return generate_credentials_key()


@pytest.fixture
def connections_settings(connections_key: str) -> Settings:
    """Settings with a fixed JWT secret and a valid credentials key."""
    return Settings(
        jwt_secret="test-secret-0123456789-abcdefghijklmnop",
        access_token_expire_minutes=30,
        credentials_key=connections_key,
    )


@pytest.fixture
def connections_app(
    seeded_db: Database, connections_settings: Settings, demo_catalog: ModuleCatalog
) -> FastAPI:
    """App whose catalog holds the test manifests and settings hold a Fernet key."""
    application: FastAPI = create_app()
    application.state.db = seeded_db
    application.state.settings = connections_settings
    application.state.module_catalog = demo_catalog
    return application


@pytest.fixture
async def connections_client(connections_app: FastAPI) -> AsyncClient:
    """HTTP client bound to :func:`connections_app`."""
    transport = ASGITransport(app=connections_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
def no_key_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Development settings without a credentials key (fail-closed scenario)."""
    monkeypatch.delenv("AA_CREDENTIALS_KEY", raising=False)
    return Settings(
        jwt_secret="test-secret-0123456789-abcdefghijklmnop",
        access_token_expire_minutes=30,
        credentials_key=None,
    )


@pytest.fixture
def no_key_app(
    seeded_db: Database, no_key_settings: Settings, demo_catalog: ModuleCatalog
) -> FastAPI:
    """App without a credentials key: encryption must fail closed, not store."""
    application: FastAPI = create_app()
    application.state.db = seeded_db
    application.state.settings = no_key_settings
    application.state.module_catalog = demo_catalog
    return application


@pytest.fixture
async def no_key_client(no_key_app: FastAPI) -> AsyncClient:
    """HTTP client bound to :func:`no_key_app`."""
    transport = ASGITransport(app=no_key_app)
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


async def _admin(client: AsyncClient) -> dict[str, str]:
    return await _headers(client, "admin@acme.test", "admin-pass")


async def _viewer(client: AsyncClient) -> dict[str, str]:
    return await _headers(client, "viewer@acme.test", "viewer-pass")


async def _globex_owner(client: AsyncClient) -> dict[str, str]:
    return await _headers(client, "owner@globex.test", "other-pass", "globex")


async def _connection(db: Database, connection_id: int) -> UserConnection | None:
    """Load one connection row by id (tenant filters belong to the caller)."""
    async with db.sessionmaker() as session:
        return await session.get(UserConnection, connection_id)


async def _count(db: Database, **filters) -> int:
    """Count connection rows matching exact column values."""
    async with db.sessionmaker() as session:
        query = select(func.count()).select_from(UserConnection)
        for name, value in filters.items():
            query = query.where(getattr(UserConnection, name) == value)
        return int(await session.scalar(query))


async def _create(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    credentials=None,
    module_key: str = CALENDAR,
    provider: str = PROVIDER,
    metadata: dict | None = None,
):
    """POST a connection as the given principal; asserts the happy 200."""
    payload: dict = {"module_key": module_key, "provider": provider}
    if credentials is not None:
        payload["credentials"] = credentials
    if metadata is not None:
        payload["metadata"] = metadata
    response = await client.post(BASE, json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response


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


async def test_endpoints_require_authentication(connections_client: AsyncClient) -> None:
    """Every connections route rejects anonymous callers."""
    assert (await connections_client.get(BASE)).status_code == 401
    assert (await connections_client.get(f"{BASE}/tenant")).status_code == 401
    assert (await connections_client.post(BASE)).status_code == 401
    assert (await connections_client.get(f"{BASE}/1")).status_code == 401
    assert (await connections_client.delete(f"{BASE}/1")).status_code == 401
    assert (await connections_client.delete(f"{BASE}/1/revoke")).status_code == 401


async def test_create_with_credentials_encrypts_and_never_returns_them(
    connections_client: AsyncClient,
    seeded_db: Database,
    seeded_users,
    connections_key: str,
) -> None:
    """Credentials are encrypted at rest and absent from write and read bodies."""
    headers = await _owner(connections_client)
    owner = seeded_users["owner@acme.test"]

    response = await connections_client.post(
        BASE,
        json={
            "module_key": CALENDAR,
            "provider": PROVIDER,
            "credentials": CREDS,
            "metadata": {"label": "Work calendar"},
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "connected"
    assert body["user_id"] == owner.id
    assert body["metadata"] == {"label": "Work calendar"}
    assert "credentials" not in body
    assert SECRET not in response.text

    row = await _connection(seeded_db, body["id"])
    assert row is not None
    assert row.credentials_encrypted
    assert SECRET not in row.credentials_encrypted
    assert json.loads(decrypt_credentials(connections_key, row.credentials_encrypted)) == CREDS

    # The stored token and the plaintext stay out of every subsequent read.
    for path in (BASE, f"{BASE}/{body['id']}"):
        read = await connections_client.get(path, headers=headers)
        assert read.status_code == 200, read.text
        assert "credentials" not in read.text
        assert SECRET not in read.text
        assert row.credentials_encrypted not in read.text


async def test_create_without_credentials_stays_pending(
    connections_client: AsyncClient, seeded_db: Database
) -> None:
    """A connection created with no credentials is pending with no stored blob."""
    headers = await _owner(connections_client)

    response = await _create(connections_client, headers)

    body = response.json()
    assert body["status"] == "pending"
    assert body["metadata"] == {}
    assert body["last_health_at"] is None
    assert "credentials" not in response.text
    row = await _connection(seeded_db, body["id"])
    assert row is not None
    assert row.credentials_encrypted is None


async def test_unknown_module_key_is_rejected_before_any_write(
    connections_client: AsyncClient, seeded_db: Database
) -> None:
    """A module outside the catalog is 404 and creates no row."""
    headers = await _owner(connections_client)

    response = await connections_client.post(
        BASE,
        json={"module_key": "ghost_module", "provider": PROVIDER},
        headers=headers,
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "module not found"
    assert await _count(seeded_db, module_key="ghost_module") == 0


async def test_provider_must_be_a_lowercase_identifier(
    connections_client: AsyncClient, seeded_db: Database
) -> None:
    """Uppercase or spaced providers are 422, never stored as-is."""
    headers = await _owner(connections_client)

    for provider in ("CalDAV", "cald av", "Caldav2"):
        response = await connections_client.post(
            BASE,
            json={"module_key": CALENDAR, "provider": provider},
            headers=headers,
        )
        assert response.status_code == 422, provider

    assert await _count(seeded_db) == 0


async def test_foreign_tenant_ids_answer_not_found(
    connections_client: AsyncClient, seeded_db: Database
) -> None:
    """Another tenant sees 404 for every route; unknown ids look identical."""
    acme = await _owner(connections_client)
    globex = await _globex_owner(connections_client)
    connection_id = (await _create(connections_client, acme, credentials=SECRET)).json()["id"]

    for method, path in (
        ("GET", f"{BASE}/{connection_id}"),
        ("DELETE", f"{BASE}/{connection_id}"),
        ("DELETE", f"{BASE}/{connection_id}/revoke"),
    ):
        response = await connections_client.request(method, path, headers=globex)
        assert response.status_code == 404, path

    foreign = await connections_client.get(f"{BASE}/{connection_id}", headers=globex)
    unknown = await connections_client.get(f"{BASE}/{connection_id + 10_000}", headers=globex)
    assert foreign.json() == unknown.json()

    listed = await connections_client.get(BASE, headers=globex)
    assert listed.json()["connections"] == []


async def test_same_tenant_users_cannot_touch_each_others_connections(
    connections_client: AsyncClient, seeded_db: Database, connections_key: str
) -> None:
    """A second user of one tenant gets 404s and only ever writes their own row."""
    owner_headers = await _owner(connections_client)
    viewer_headers = await _viewer(connections_client)
    created = await _create(connections_client, owner_headers, credentials=SECRET)
    owner_body = created.json()
    connection_id = owner_body["id"]
    owner_row = await _connection(seeded_db, connection_id)
    blob_before = owner_row.credentials_encrypted

    get_attempt = await connections_client.get(f"{BASE}/{connection_id}", headers=viewer_headers)
    delete_attempt = await connections_client.delete(
        f"{BASE}/{connection_id}", headers=viewer_headers
    )
    unknown = await connections_client.get(
        f"{BASE}/{connection_id + 10_000}", headers=viewer_headers
    )

    assert get_attempt.status_code == 404
    assert get_attempt.json() == unknown.json()
    assert delete_attempt.status_code == 404
    # The viewer is no admin: revoke is refused before the resource resolves.
    revoke_attempt = await connections_client.delete(
        f"{BASE}/{connection_id}/revoke", headers=viewer_headers
    )
    assert revoke_attempt.status_code == 403
    assert revoke_attempt.json()["detail"] == "forbidden"

    # A create-or-update by the viewer makes the viewer's own row only.
    own = (await _create(connections_client, viewer_headers, credentials=ROTATED_SECRET)).json()
    assert own["user_id"] != owner_body["user_id"]
    assert own["id"] != connection_id
    owner_row_after = await _connection(seeded_db, connection_id)
    assert owner_row_after.credentials_encrypted == blob_before
    assert owner_row_after.status is ConnectionStatus.CONNECTED
    assert await _count(seeded_db, module_key=CALENDAR, provider=PROVIDER) == 2
    assert decrypt_credentials(connections_key, blob_before) == SECRET


async def test_owner_listing_returns_only_own_connections(
    connections_client: AsyncClient,
) -> None:
    """``GET /`` is always the caller's own list, whatever their role."""
    owner_headers = await _owner(connections_client)
    viewer_headers = await _viewer(connections_client)
    owner_id = (await _create(connections_client, owner_headers, credentials=SECRET)).json()["id"]
    viewer_id = (await _create(connections_client, viewer_headers)).json()["id"]

    owner_list = await connections_client.get(BASE, headers=owner_headers)
    viewer_list = await connections_client.get(BASE, headers=viewer_headers)

    assert [item["id"] for item in owner_list.json()["connections"]] == [owner_id]
    assert [item["id"] for item in viewer_list.json()["connections"]] == [viewer_id]
    assert all(item["user_id"] == owner_id for item in owner_list.json()["connections"])


async def test_admin_tenant_listing_shows_metadata_without_secrets(
    connections_client: AsyncClient, seeded_db: Database, seeded_users, connections_key: str
) -> None:
    """The admin list spans the tenant's users and never carries credentials."""
    owner_headers = await _owner(connections_client)
    viewer_headers = await _viewer(connections_client)
    globex_headers = await _globex_owner(connections_client)
    owner_conn_id = (await _create(connections_client, owner_headers, credentials=SECRET)).json()[
        "id"
    ]
    viewer_conn_id = (
        await _create(connections_client, viewer_headers, credentials=ROTATED_SECRET)
    ).json()["id"]
    globex_conn_id = (await _create(connections_client, globex_headers, credentials=SECRET)).json()[
        "id"
    ]

    response = await connections_client.get(
        f"{BASE}/tenant", headers=await _admin(connections_client)
    )

    assert response.status_code == 200, response.text
    items = response.json()["connections"]
    assert {item["id"] for item in items} == {owner_conn_id, viewer_conn_id}
    assert globex_conn_id not in {item["id"] for item in items}
    assert {item["user_id"] for item in items} == {
        seeded_users["owner@acme.test"].id,
        seeded_users["viewer@acme.test"].id,
    }
    assert all(item["status"] == "connected" for item in items)
    assert "credentials" not in response.text
    assert SECRET not in response.text
    owner_row = await _connection(seeded_db, owner_conn_id)
    assert owner_row.credentials_encrypted not in response.text


async def test_admin_scope_requires_owner_or_admin(
    connections_client: AsyncClient,
) -> None:
    """The tenant listing and revoke routes are owner/admin only."""
    viewer_headers = await _viewer(connections_client)
    connection_id = (await _create(connections_client, viewer_headers, credentials=SECRET)).json()[
        "id"
    ]

    listed = await connections_client.get(f"{BASE}/tenant", headers=viewer_headers)
    revoked = await connections_client.delete(
        f"{BASE}/{connection_id}/revoke", headers=viewer_headers
    )

    assert listed.status_code == 403
    assert listed.json()["detail"] == "forbidden"
    assert revoked.status_code == 403
    assert revoked.json()["detail"] == "forbidden"


async def test_repeated_create_updates_credentials_in_place(
    connections_client: AsyncClient,
    seeded_db: Database,
    connections_key: str,
) -> None:
    """Create-or-update keys on (user, module, provider): one row, fresh secret."""
    headers = await _owner(connections_client)

    first = (await _create(connections_client, headers, credentials=SECRET)).json()
    second = (await _create(connections_client, headers, credentials=ROTATED_SECRET)).json()

    assert second["id"] == first["id"]
    assert await _count(seeded_db, module_key=CALENDAR, provider=PROVIDER) == 1
    row = await _connection(seeded_db, first["id"])
    assert row.status is ConnectionStatus.CONNECTED
    assert decrypt_credentials(connections_key, row.credentials_encrypted) == ROTATED_SECRET

    # A metadata-only update keeps the stored secret untouched.
    third = (await _create(connections_client, headers, metadata={"label": "renamed"})).json()
    assert third["id"] == first["id"]
    row_after = await _connection(seeded_db, first["id"])
    assert row_after.credentials_encrypted == row.credentials_encrypted
    assert row_after.metadata_ == {"label": "renamed"}
    assert third["metadata"] == {"label": "renamed"}


async def test_admin_revoke_destroys_the_stored_blob(
    connections_client: AsyncClient, seeded_db: Database, audit_log
) -> None:
    """Revoke sets ``revoked``, wipes the ciphertext, and audits without secrets."""
    owner_headers = await _owner(connections_client)
    admin_headers = await _admin(connections_client)
    connection_id = (await _create(connections_client, owner_headers, credentials=CREDS)).json()[
        "id"
    ]
    token_before = (await _connection(seeded_db, connection_id)).credentials_encrypted
    assert token_before

    response = await connections_client.delete(
        f"{BASE}/{connection_id}/revoke", headers=admin_headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "revoked"
    assert "credentials" not in body
    assert SECRET not in response.text

    row = await _connection(seeded_db, connection_id)
    assert row.status is ConnectionStatus.REVOKED
    assert row.credentials_encrypted is None

    events = await audit_log(seeded_db, action="connection.revoked")
    assert len(events) == 1
    event = events[0]
    assert event.payload == {"module_key": CALENDAR, "provider": PROVIDER, "status": "revoked"}
    rendered = _rendered_event(event)
    assert SECRET not in rendered
    assert token_before not in rendered

    # Revoking an already-revoked connection is the same 200, audited once.
    again = await connections_client.delete(f"{BASE}/{connection_id}/revoke", headers=admin_headers)
    assert again.status_code == 200
    assert again.json()["status"] == "revoked"
    assert len(await audit_log(seeded_db, action="connection.revoked")) == 1


async def test_create_audit_event_carries_only_the_public_triple(
    connections_client: AsyncClient, seeded_db: Database, seeded_users, audit_log
) -> None:
    """``connection.created`` payload is {module_key, provider, status} — nothing else."""
    owner = seeded_users["owner@acme.test"]
    headers = await _owner(connections_client)
    connection_id = (await _create(connections_client, headers, credentials=CREDS)).json()["id"]
    token = (await _connection(seeded_db, connection_id)).credentials_encrypted

    events = await audit_log(seeded_db, action="connection.created")

    assert len(events) == 1
    event = events[0]
    assert event.tenant_id == owner.tenant_id
    assert event.actor_user_id == owner.id
    assert event.resource_type == "connection"
    assert event.resource_id == str(connection_id)
    assert event.payload == {"module_key": CALENDAR, "provider": PROVIDER, "status": "connected"}
    rendered = _rendered_event(event)
    assert SECRET not in rendered
    assert token not in rendered


async def test_delete_removes_the_own_connection_and_audits(
    connections_client: AsyncClient, seeded_db: Database, audit_log
) -> None:
    """DELETE removes the caller's row (204) and audits the pre-delete state."""
    headers = await _owner(connections_client)
    connection_id = (await _create(connections_client, headers, credentials=CREDS)).json()["id"]

    response = await connections_client.delete(f"{BASE}/{connection_id}", headers=headers)

    assert response.status_code == 204
    assert response.text == ""
    assert await _connection(seeded_db, connection_id) is None
    assert await _count(seeded_db, module_key=CALENDAR, provider=PROVIDER) == 0

    events = await audit_log(seeded_db, action="connection.deleted")
    assert len(events) == 1
    assert events[0].payload == {
        "module_key": CALENDAR,
        "provider": PROVIDER,
        "status": "connected",
    }
    assert SECRET not in _rendered_event(events[0])


async def test_missing_credentials_key_fails_closed_and_stores_nothing(
    no_key_client: AsyncClient, seeded_db: Database
) -> None:
    """Without a key the write path refuses (500) instead of storing plaintext."""
    headers = await _owner(no_key_client)

    refused = await no_key_client.post(
        BASE,
        json={"module_key": CALENDAR, "provider": PROVIDER, "credentials": SECRET},
        headers=headers,
    )

    assert refused.status_code == 500
    assert refused.json()["detail"] == "credentials encryption is not configured"
    assert await _count(seeded_db, module_key=CALENDAR, provider=PROVIDER) == 0
    assert SECRET not in refused.text

    # Outside production the key is optional: no credentials, no problem.
    pending = await no_key_client.post(
        BASE, json={"module_key": CALENDAR, "provider": PROVIDER}, headers=headers
    )
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"


async def test_credentials_never_reach_application_logs(
    connections_client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A create/read round trip logs nothing that contains the plaintext."""
    headers = await _owner(connections_client)

    with caplog.at_level(logging.DEBUG):
        created = await _create(connections_client, headers, credentials=SECRET)
        read = await connections_client.get(f"{BASE}/{created.json()['id']}", headers=headers)

    assert read.status_code == 200
    assert SECRET not in caplog.text


async def test_set_status_marks_health_failures_for_later_use(
    seeded_db: Database, seeded_users, demo_catalog: ModuleCatalog, connections_key: str
) -> None:
    """The T4 health seam flips status and 404s on foreign tenants."""
    owner = seeded_users["owner@acme.test"]

    async with seeded_db.sessionmaker() as session:
        created = await create_or_update(
            session,
            catalog=demo_catalog,
            credentials_key=connections_key,
            caller=owner,
            module_key=CALENDAR,
            provider=PROVIDER,
            credentials=SECRET,
        )
        expired = await set_status(
            session,
            tenant_id=owner.tenant_id,
            connection_id=created.id,
            status=ConnectionStatus.EXPIRED,
        )
        assert expired.status is ConnectionStatus.EXPIRED

        failed = await set_status(
            session,
            tenant_id=owner.tenant_id,
            connection_id=created.id,
            status=ConnectionStatus.FAILED,
        )
        assert failed.status is ConnectionStatus.FAILED

        with pytest.raises(ConnectionNotFoundError):
            await set_status(
                session,
                tenant_id=999_999,
                connection_id=created.id,
                status=ConnectionStatus.EXPIRED,
            )


async def test_admin_get_and_replace_over_a_tenant_connection(
    seeded_db: Database, seeded_users, demo_catalog: ModuleCatalog, connections_key: str
) -> None:
    """Admin service reads/replaces within the tenant; secrets keep the rules."""
    owner = seeded_users["owner@acme.test"]
    admin = seeded_users["admin@acme.test"]
    globex = seeded_users["owner@globex.test"]

    async with seeded_db.sessionmaker() as session:
        created = await create_or_update(
            session,
            catalog=demo_catalog,
            credentials_key=connections_key,
            caller=owner,
            module_key=CALENDAR,
            provider=PROVIDER,
            credentials=SECRET,
            metadata={"label": "A"},
        )

        fetched = await get_for_tenant(session, tenant_id=owner.tenant_id, connection_id=created.id)
        assert fetched.id == created.id
        with pytest.raises(ConnectionNotFoundError):
            await get_for_tenant(session, tenant_id=globex.tenant_id, connection_id=created.id)

        # A second row for the same user makes a natural-key collision possible.
        await create_or_update(
            session,
            catalog=demo_catalog,
            credentials_key=connections_key,
            caller=owner,
            module_key=MAIL,
            provider="imap",
            credentials=ROTATED_SECRET,
        )

        # Replace without credentials: display fields change, secret is kept.
        replaced = await replace(
            session,
            catalog=demo_catalog,
            credentials_key=connections_key,
            caller=admin,
            connection_id=created.id,
            module_key=CALENDAR,
            provider=PROVIDER,
            metadata={"label": "Renamed"},
        )
        assert replaced.metadata_ == {"label": "Renamed"}
        assert replaced.status is ConnectionStatus.CONNECTED
        assert decrypt_credentials(connections_key, replaced.credentials_encrypted) == SECRET

        with pytest.raises(ConnectionConflictError):
            await replace(
                session,
                catalog=demo_catalog,
                credentials_key=connections_key,
                caller=admin,
                connection_id=created.id,
                module_key=MAIL,
                provider="imap",
                metadata={"label": "collide"},
            )

        reconnected = await replace(
            session,
            catalog=demo_catalog,
            credentials_key=connections_key,
            caller=admin,
            connection_id=created.id,
            module_key=CALENDAR,
            provider=PROVIDER,
            metadata={"label": "Renamed"},
            credentials=ROTATED_SECRET,
        )
        assert reconnected.status is ConnectionStatus.CONNECTED
        assert (
            decrypt_credentials(connections_key, reconnected.credentials_encrypted)
            == ROTATED_SECRET
        )


async def test_service_fails_closed_on_a_malformed_key(
    seeded_db: Database, seeded_users, demo_catalog: ModuleCatalog
) -> None:
    """A key that slipped past settings validation never reaches encryption."""
    owner = seeded_users["owner@acme.test"]

    async with seeded_db.sessionmaker() as session:
        with pytest.raises(CredentialsEncryptionError, match="not configured"):
            await create_or_update(
                session,
                catalog=demo_catalog,
                credentials_key="not-a-fernet-key",
                caller=owner,
                module_key=CALENDAR,
                provider=PROVIDER,
                credentials=SECRET,
            )

    assert await _count(seeded_db, module_key=CALENDAR, provider=PROVIDER) == 0
