"""Module catalog API tests: catalog reads, activation, RBAC, isolation, audit.

The app under test carries a local ``ModuleCatalog`` with two test-only
manifests (the process-wide default catalog stays empty), so activation can
be observed without shipping a real module — T1 ships no production module.

HTTP shape under test: ``GET /api/v1/modules`` is readable by any
authenticated role and reflects only the caller's tenant; the owner/admin
pair ``POST .../{key}/activate`` (set active) and
``DELETE .../{key}/activate`` (remove the activation) is idempotent,
answers 404 for unknown keys, and audits every state change.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.db import Database
from app.main import create_app
from app.modules.catalog.exceptions import UnknownModuleError
from app.modules.catalog.manifest import FieldSpec, ModuleManifest, StepSpec
from app.modules.catalog.models import ModuleActivation
from app.modules.catalog.registry import ModuleCatalog
from app.modules.catalog.service import activate, active_module_keys, deactivate

BASE = "/api/v1/modules"
CALENDAR = "demo_calendar"
MAIL = "demo_mail"


@pytest.fixture
def demo_catalog() -> ModuleCatalog:
    """Local catalog with two test-only manifests (never the default catalog)."""
    catalog = ModuleCatalog()
    catalog.register(
        ModuleManifest(
            key=CALENDAR,
            name="Demo Calendar",
            description="test-only calendar module for the catalog API",
            version="1.2.0",
            permissions=("calendar:read",),
            tools=("demo_list_events",),
            onboarding=(
                StepSpec(
                    key="connect",
                    type="form",
                    title="Connect the calendar",
                    description="Enter the CalDAV credentials",
                    fields=(
                        FieldSpec(key="endpoint", label="Server URL", type="url"),
                        FieldSpec(key="password", label="Password", type="password", secret=True),
                    ),
                ),
            ),
        )
    )
    catalog.register(
        ModuleManifest(
            key=MAIL,
            name="Demo Mail",
            description="test-only mail module for the catalog API",
            version="0.1.0",
        )
    )
    return catalog


@pytest.fixture
def catalog_app(seeded_db: Database, identity_settings, demo_catalog: ModuleCatalog) -> FastAPI:
    """App whose module catalog holds the test-only manifests."""
    application: FastAPI = create_app()
    application.state.db = seeded_db
    application.state.settings = identity_settings
    application.state.module_catalog = demo_catalog
    return application


@pytest.fixture
async def catalog_client(catalog_app: FastAPI) -> AsyncClient:
    """HTTP client bound to :func:`catalog_app`."""
    transport = ASGITransport(app=catalog_app)
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


async def _activation_count(db: Database, tenant_id: int, key: str) -> int:
    """Count the stored activation rows of one tenant and module key."""
    async with db.sessionmaker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ModuleActivation)
            .where(ModuleActivation.tenant_id == tenant_id, ModuleActivation.module_key == key)
        )
    return int(count)


async def test_module_endpoints_require_authentication(catalog_client: AsyncClient) -> None:
    """Every modules route rejects anonymous callers."""
    assert (await catalog_client.get(BASE)).status_code == 401
    assert (await catalog_client.post(f"{BASE}/{CALENDAR}/activate")).status_code == 401
    assert (await catalog_client.delete(f"{BASE}/{CALENDAR}/activate")).status_code == 401


async def test_list_returns_the_catalog_for_any_authenticated_role(
    catalog_client: AsyncClient,
) -> None:
    """A viewer reads the full catalog with its own tenant's activation flags."""
    activated = await catalog_client.post(
        f"{BASE}/{CALENDAR}/activate", headers=await _owner(catalog_client)
    )
    assert activated.status_code == 200

    response = await catalog_client.get(BASE, headers=await _viewer(catalog_client))

    assert response.status_code == 200
    body = response.json()
    assert [module["key"] for module in body["modules"]] == [CALENDAR, MAIL]
    calendar = body["modules"][0]
    assert calendar == {
        "key": CALENDAR,
        "name": "Demo Calendar",
        "description": "test-only calendar module for the catalog API",
        "version": "1.2.0",
        "permissions": ["calendar:read"],
        "tools": ["demo_list_events"],
        "active": True,
    }
    assert body["modules"][1]["active"] is False
    assert "tenant_id" not in body
    assert all("tenant_id" not in module for module in body["modules"])


async def test_list_shows_only_the_callers_tenant_activation(
    catalog_client: AsyncClient,
) -> None:
    """Activations never leak: each tenant sees exactly its own flags."""
    acme = await _owner(catalog_client)
    globex = await _globex_owner(catalog_client)
    assert (
        await catalog_client.post(f"{BASE}/{CALENDAR}/activate", headers=acme)
    ).status_code == 200
    assert (await catalog_client.post(f"{BASE}/{MAIL}/activate", headers=globex)).status_code == 200

    acme_list = (await catalog_client.get(BASE, headers=acme)).json()["modules"]
    globex_list = (await catalog_client.get(BASE, headers=globex)).json()["modules"]

    assert {m["key"]: m["active"] for m in acme_list} == {CALENDAR: True, MAIL: False}
    assert {m["key"]: m["active"] for m in globex_list} == {CALENDAR: False, MAIL: True}


async def test_unknown_module_key_answers_not_found(catalog_client: AsyncClient) -> None:
    """A key outside the catalog is 404 on both mutations, never a 500."""
    headers = await _owner(catalog_client)

    activation = await catalog_client.post(f"{BASE}/ghost_module/activate", headers=headers)
    deactivation = await catalog_client.delete(f"{BASE}/ghost_module/activate", headers=headers)

    assert activation.status_code == 404
    assert activation.json()["detail"] == "module not found"
    assert deactivation.status_code == 404


async def test_viewer_can_read_but_not_activate(catalog_client: AsyncClient) -> None:
    """The catalog is readable by every role; activation is owner/admin only."""
    headers = await _viewer(catalog_client)

    listed = await catalog_client.get(BASE, headers=headers)
    activation = await catalog_client.post(f"{BASE}/{CALENDAR}/activate", headers=headers)
    deactivation = await catalog_client.delete(f"{BASE}/{CALENDAR}/activate", headers=headers)

    assert listed.status_code == 200
    assert activation.status_code == 403
    assert activation.json()["detail"] == "forbidden"
    assert deactivation.status_code == 403


async def test_admin_role_can_activate(catalog_client: AsyncClient) -> None:
    """An admin passes the owner/admin gate just like an owner."""
    headers = await _admin(catalog_client)
    response = await catalog_client.post(f"{BASE}/{CALENDAR}/activate", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"key": CALENDAR, "active": True}


async def test_activate_and_deactivate_are_idempotent(
    catalog_client: AsyncClient, seeded_db: Database, seeded_users
) -> None:
    """Repeats keep returning 200 with the same state and never duplicate rows."""
    owner = seeded_users["owner@acme.test"]
    headers = await _owner(catalog_client)

    created = await catalog_client.post(f"{BASE}/{CALENDAR}/activate", headers=headers)
    repeated = await catalog_client.post(f"{BASE}/{CALENDAR}/activate", headers=headers)
    assert created.status_code == repeated.status_code == 200
    assert created.json() == repeated.json() == {"key": CALENDAR, "active": True}
    assert "tenant_id" not in created.json()
    assert await _activation_count(seeded_db, owner.tenant_id, CALENDAR) == 1

    removed = await catalog_client.delete(f"{BASE}/{CALENDAR}/activate", headers=headers)
    removed_again = await catalog_client.delete(f"{BASE}/{CALENDAR}/activate", headers=headers)
    assert removed.status_code == removed_again.status_code == 200
    assert removed.json() == removed_again.json() == {"key": CALENDAR, "active": False}
    assert await _activation_count(seeded_db, owner.tenant_id, CALENDAR) == 1

    reactivated = await catalog_client.post(f"{BASE}/{CALENDAR}/activate", headers=headers)
    assert reactivated.status_code == 200
    assert reactivated.json() == {"key": CALENDAR, "active": True}
    assert await _activation_count(seeded_db, owner.tenant_id, CALENDAR) == 1


async def test_deactivating_a_never_activated_module_is_a_clean_noop(
    catalog_client: AsyncClient, seeded_db: Database, seeded_users, audit_log
) -> None:
    """DELETE for a module this tenant never activated: 200, no row, no event."""
    owner = seeded_users["owner@acme.test"]
    headers = await _owner(catalog_client)

    response = await catalog_client.delete(f"{BASE}/{MAIL}/activate", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"key": MAIL, "active": False}
    assert await _activation_count(seeded_db, owner.tenant_id, MAIL) == 0
    assert await audit_log(seeded_db, action="module.deactivated") == []


async def test_activation_changes_are_audited_without_secrets(
    catalog_client: AsyncClient, seeded_db: Database, seeded_users, audit_log
) -> None:
    """Each real state change writes its event; idempotent repeats write none."""
    owner = seeded_users["owner@acme.test"]
    headers = await _owner(catalog_client)

    assert (
        await catalog_client.post(f"{BASE}/{CALENDAR}/activate", headers=headers)
    ).status_code == 200
    assert (
        await catalog_client.post(f"{BASE}/{CALENDAR}/activate", headers=headers)
    ).status_code == 200

    activated = await audit_log(seeded_db, action="module.activated")
    assert len(activated) == 1
    event = activated[0]
    assert event.tenant_id == owner.tenant_id
    assert event.actor_user_id == owner.id
    assert event.resource_type == "module"
    assert event.resource_id == CALENDAR
    assert event.payload == {"key": CALENDAR, "version": "1.2.0"}

    assert (
        await catalog_client.delete(f"{BASE}/{CALENDAR}/activate", headers=headers)
    ).status_code == 200

    deactivated = await audit_log(seeded_db, action="module.deactivated")
    assert len(deactivated) == 1
    assert deactivated[0].tenant_id == owner.tenant_id
    assert deactivated[0].actor_user_id == owner.id
    assert deactivated[0].resource_type == "module"
    assert deactivated[0].resource_id == CALENDAR
    assert deactivated[0].payload == {"key": CALENDAR, "version": "1.2.0"}


async def test_active_module_keys_tracks_only_active_tenant_rows(
    seeded_db: Database, seeded_users, demo_catalog: ModuleCatalog
) -> None:
    """The T2 integration helper returns active keys per tenant, nothing else."""
    owner = seeded_users["owner@acme.test"]
    globex = seeded_users["owner@globex.test"]

    async with seeded_db.sessionmaker() as session:
        assert await active_module_keys(session, owner.tenant_id) == set()

        await activate(session, demo_catalog, owner, CALENDAR)
        assert await active_module_keys(session, owner.tenant_id) == {CALENDAR}
        assert await active_module_keys(session, globex.tenant_id) == set()

        await deactivate(session, demo_catalog, owner, CALENDAR)
        assert await active_module_keys(session, owner.tenant_id) == set()


async def test_activate_service_rejects_unknown_keys(
    seeded_db: Database, seeded_users, demo_catalog: ModuleCatalog
) -> None:
    """The service refuses keys outside the catalog before touching the database."""
    owner = seeded_users["owner@acme.test"]

    async with seeded_db.sessionmaker() as session:
        with pytest.raises(UnknownModuleError, match="unknown module"):
            await activate(session, demo_catalog, owner, "ghost_module")
        assert await active_module_keys(session, owner.tenant_id) == set()
