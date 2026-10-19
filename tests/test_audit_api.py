"""Audit API tests: role gating, tenant isolation, filters, and pagination."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app
from app.modules.approvals.service import create_approval_request
from app.modules.audit.models import ActorType
from app.modules.audit.service import persist_event
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password

BASE = "/api/v1/audit"
T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


@pytest.fixture
async def audit_client(
    seeded_db: Database, identity_settings: Settings, approval_tools
) -> AsyncClient:
    """HTTP client bound to an app on the seeded database and test-only tools."""
    application: FastAPI = create_app()
    application.state.db = seeded_db
    application.state.settings = identity_settings
    application.state.tool_registry = approval_tools.registry
    transport = ASGITransport(app=application)
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


async def _record(db: Database, tenant_id: int, action: str, **fields) -> str:
    async with db.sessionmaker() as session:
        event = await persist_event(session, tenant_id=tenant_id, action=action, **fields)
        return str(event.id)


async def _list(client: AsyncClient, headers: dict[str, str], **params) -> list[dict]:
    response = await client.get(BASE, params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["events"]


async def test_requires_authentication(audit_client: AsyncClient) -> None:
    """Anonymous callers get 401."""
    assert (await audit_client.get(BASE)).status_code == 401


async def test_viewer_and_operator_are_forbidden(
    audit_client: AsyncClient, seeded_db: Database, seeded_users
) -> None:
    """Only owner/admin may read the audit trail."""
    acme = seeded_users["owner@acme.test"].tenant_id
    async with seeded_db.sessionmaker() as session:
        session.add(
            User(
                tenant_id=acme,
                email="operator@acme.test",
                full_name="Acme Operator",
                hashed_password=hash_password("operator-pass"),
                role=UserRole.OPERATOR,
            )
        )
        await session.commit()

    for email, password in [
        ("viewer@acme.test", "viewer-pass"),
        ("operator@acme.test", "operator-pass"),
    ]:
        headers = await _headers(audit_client, email, password)
        response = await audit_client.get(BASE, headers=headers)
        assert response.status_code == 403, email
        assert response.json() == {"detail": "forbidden"}


async def test_owner_and_admin_see_only_their_tenant(
    audit_client: AsyncClient, seeded_db: Database, seeded_users
) -> None:
    """Another tenant's events never appear; ``tenant_id`` is not exposed."""
    acme = seeded_users["owner@acme.test"].tenant_id
    globex = seeded_users["owner@globex.test"].tenant_id
    acme_id = await _record(seeded_db, acme, "tool.executed", tool_name="peek_note")
    globex_id = await _record(seeded_db, globex, "tool.executed", tool_name="peek_note")

    for headers in [
        await _owner(audit_client),
        await _headers(audit_client, "admin@acme.test", "admin-pass"),
    ]:
        events = await _list(audit_client, headers)
        assert [event["id"] for event in events] == [acme_id]
        assert "tenant_id" not in events[0]
    foreign = await _headers(audit_client, "owner@globex.test", "other-pass", "globex")
    assert [event["id"] for event in await _list(audit_client, foreign)] == [globex_id]


async def test_superadmin_passes_the_role_check_but_not_the_tenant(
    audit_client: AsyncClient, seeded_db: Database, seeded_users
) -> None:
    """A superadmin viewer reads the audit trail of their own tenant only."""
    viewer = seeded_users["viewer@acme.test"]
    globex = seeded_users["owner@globex.test"].tenant_id
    own_id = await _record(seeded_db, viewer.tenant_id, "approval.created")
    await _record(seeded_db, globex, "approval.created")
    async with seeded_db.sessionmaker() as session:
        await session.execute(update(User).where(User.id == viewer.id).values(is_superadmin=True))
        await session.commit()

    headers = await _headers(audit_client, "viewer@acme.test", "viewer-pass")

    assert [event["id"] for event in await _list(audit_client, headers)] == [own_id]


async def test_filters_narrow_the_listing(
    audit_client: AsyncClient, seeded_db: Database, seeded_users
) -> None:
    """Actor, action (exact/prefix), tool, resource, correlation, and time filters."""
    owner, admin = seeded_users["owner@acme.test"], seeded_users["admin@acme.test"]
    acme = owner.tenant_id
    executed = await _record(
        seeded_db,
        acme,
        "tool.executed",
        actor_user_id=owner.id,
        tool_name="peek_note",
        correlation_id="chat-1",
        now=T0,
    )
    denied = await _record(
        seeded_db,
        acme,
        "tool.denied",
        actor_user_id=admin.id,
        tool_name="send_note",
        now=T0 + timedelta(hours=1),
    )
    created = await _record(
        seeded_db,
        acme,
        "approval.created",
        actor_user_id=admin.id,
        tool_name="send_note",
        resource_type="approval_request",
        resource_id="req-1",
        correlation_id="req-1",
        now=T0 + timedelta(hours=2),
    )
    headers = await _owner(audit_client)

    async def ids(**params) -> list[str]:
        return [event["id"] for event in await _list(audit_client, headers, **params)]

    assert await ids() == [created, denied, executed]
    assert await ids(actor_user_id=admin.id) == [created, denied]
    assert await ids(action="tool.denied") == [denied]
    assert await ids(action_prefix="tool.") == [denied, executed]
    assert await ids(tool_name="send_note") == [created, denied]
    assert await ids(resource_type="approval_request", resource_id="req-1") == [created]
    assert await ids(correlation_id="chat-1") == [executed]
    window = {
        "created_from": (T0 + timedelta(minutes=30)).isoformat(),
        "created_to": (T0 + timedelta(hours=2)).isoformat(),
    }
    assert await ids(**window) == [denied]


async def test_pagination_is_bounded_and_walks_every_event_once(
    audit_client: AsyncClient, seeded_db: Database, seeded_users
) -> None:
    """``limit`` is capped and ``next_cursor`` pages through newest-first rows."""
    acme = seeded_users["owner@acme.test"].tenant_id
    recorded = [
        await _record(seeded_db, acme, "tool.executed", now=T0 + timedelta(minutes=minute))
        for minute in range(5)
    ]
    headers = await _owner(audit_client)

    seen: list[str] = []
    params: dict = {"limit": 2}
    while True:
        response = await audit_client.get(BASE, params=params, headers=headers)
        body = response.json()
        assert len(body["events"]) <= 2
        seen.extend(event["id"] for event in body["events"])
        if body["next_cursor"] is None:
            break
        params = {"limit": 2, "cursor": body["next_cursor"]}

    assert seen == list(reversed(recorded))
    for bad in [{"limit": 0}, {"limit": 201}, {"cursor": "garbage!"}]:
        response = await audit_client.get(BASE, params=bad, headers=headers)
        assert response.status_code == 422, bad


async def test_naive_time_bounds_are_rejected(audit_client: AsyncClient) -> None:
    """Time filters must carry an offset; ambiguous local times are refused."""
    headers = await _owner(audit_client)
    response = await audit_client.get(
        BASE, params={"created_from": "2026-10-13T09:00:00"}, headers=headers
    )
    assert response.status_code == 422


async def test_http_approval_decision_is_visible_in_the_audit_trail(
    audit_client: AsyncClient, seeded_db: Database, seeded_users, approval_tools
) -> None:
    """End to end: an approval decided over HTTP is audited with its actor."""
    admin = seeded_users["admin@acme.test"]
    async with seeded_db.sessionmaker() as session:
        request = await create_approval_request(
            session,
            approval_tools.registry,
            admin,
            "send_note",
            {"to": "ops@acme.test", "body": "Quarterly report is ready"},
            settings=Settings(),
        )
        await session.commit()
    headers = await _owner(audit_client)

    response = await audit_client.post(f"/api/v1/approvals/{request.id}/approve", headers=headers)
    assert response.status_code == 200

    events = await _list(audit_client, headers, correlation_id=str(request.id))
    assert [event["action"] for event in events] == ["approval.approved", "approval.created"]
    approved = events[0]
    assert approved["actor_user_id"] == seeded_users["owner@acme.test"].id
    assert approved["actor_type"] == ActorType.USER.value
    assert approved["resource_type"] == "approval_request"
    assert approved["resource_id"] == str(request.id)
