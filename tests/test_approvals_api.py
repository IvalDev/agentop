"""Approvals API tests: auth, tenant isolation, roles, state conflicts, expiry.

Requests are created through the service (the T9 agent's path; there is no
HTTP create endpoint) against the test-only ``send_note`` tool registered on
a local registry that replaces ``app.state.tool_registry``.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app
from app.modules.approvals.models import ApprovalRequest
from app.modules.approvals.service import approve_request, create_approval_request

TTL_SECONDS = 3600
APPROVAL_SETTINGS = Settings(approval_ttl_seconds=TTL_SECONDS)
NOTE = {"to": "ops@acme.test", "body": "Quarterly report is ready"}
#: ``route_note`` input that only an owner may approve (``note_approver`` in conftest).
OWNER_TIER_NOTE = {**NOTE, "priority": 5}
BASE = "/api/v1/approvals"


@pytest.fixture
def approvals_app(seeded_db: Database, identity_settings: Settings, approval_tools) -> FastAPI:
    """App whose tool registry holds the test-only tools."""
    application: FastAPI = create_app()
    application.state.db = seeded_db
    application.state.settings = identity_settings
    application.state.tool_registry = approval_tools.registry
    return application


@pytest.fixture
async def approvals_client(approvals_app: FastAPI) -> AsyncClient:
    """HTTP client bound to :func:`approvals_app`."""
    transport = ASGITransport(app=approvals_app)
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


async def _create(
    db: Database,
    tools,
    user,
    *,
    now: datetime | None = None,
    approve_at: datetime | None = None,
    tool: str = "send_note",
    raw_input: dict = NOTE,
) -> UUID:
    """Create (and optionally approve) a ``tool`` request via the service."""
    async with db.sessionmaker() as session:
        request = await create_approval_request(
            session, tools.registry, user, tool, raw_input, settings=APPROVAL_SETTINGS, now=now
        )
        await session.commit()
        if approve_at is not None:
            await approve_request(session, user, request.id, now=approve_at)
        return request.id


async def test_endpoints_require_authentication(approvals_client: AsyncClient) -> None:
    """Every approvals route rejects anonymous callers."""
    request_id = uuid4()
    assert (await approvals_client.get(BASE)).status_code == 401
    assert (await approvals_client.get(f"{BASE}/{request_id}")).status_code == 401
    for action in ("approve", "reject", "execute"):
        response = await approvals_client.post(f"{BASE}/{request_id}/{action}")
        assert response.status_code == 401, action


async def test_list_is_tenant_scoped_and_filters_by_status(
    approvals_client: AsyncClient, seeded_db: Database, seeded_users, approval_tools
) -> None:
    """Listing shows only the caller's tenant and honors the status filter."""
    owner = seeded_users["owner@acme.test"]
    pending_id = await _create(seeded_db, approval_tools, owner)
    approved_id = await _create(seeded_db, approval_tools, owner, approve_at=datetime.now(UTC))
    headers = await _admin(approvals_client)

    everything = await approvals_client.get(BASE, headers=headers)
    pending = await approvals_client.get(BASE, params={"status": "pending"}, headers=headers)
    foreign = await approvals_client.get(
        BASE, headers=await _headers(approvals_client, "owner@globex.test", "other-pass", "globex")
    )
    invalid = await approvals_client.get(BASE, params={"status": "bogus"}, headers=headers)

    assert everything.status_code == 200
    assert {item["id"] for item in everything.json()["approvals"]} == {
        str(pending_id),
        str(approved_id),
    }
    assert [item["id"] for item in pending.json()["approvals"]] == [str(pending_id)]
    assert foreign.json()["approvals"] == []
    assert invalid.status_code == 422


async def test_get_exposes_binding_but_not_tenant(
    approvals_client: AsyncClient, seeded_db: Database, seeded_users, approval_tools
) -> None:
    """A reviewer sees exactly what will run; tenant ids stay server-internal."""
    request_id = await _create(seeded_db, approval_tools, seeded_users["admin@acme.test"])

    response = await approvals_client.get(
        f"{BASE}/{request_id}", headers=await _owner(approvals_client)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tool_name"] == "send_note"
    assert body["input"] == {**NOTE, "priority": 1}
    assert body["action_key"].startswith("send_note:")
    assert body["status"] == "pending"
    assert body["required_role"] == "admin"
    assert "tenant_id" not in body


async def test_other_tenant_ids_are_not_found(
    approvals_client: AsyncClient, seeded_db: Database, seeded_users, approval_tools
) -> None:
    """Foreign ids look exactly like unknown ids: 404 on every route."""
    request_id = await _create(seeded_db, approval_tools, seeded_users["owner@acme.test"])
    headers = await _headers(approvals_client, "owner@globex.test", "other-pass", "globex")

    assert (await approvals_client.get(f"{BASE}/{request_id}", headers=headers)).status_code == 404
    for action in ("approve", "reject", "execute"):
        response = await approvals_client.post(f"{BASE}/{request_id}/{action}", headers=headers)
        assert response.status_code == 404, action
    assert (await approvals_client.get(f"{BASE}/{uuid4()}", headers=headers)).status_code == 404


async def test_reads_require_the_tool_role(
    approvals_client: AsyncClient, seeded_db: Database, seeded_users, approval_tools
) -> None:
    """A viewer neither lists nor gets operator-level requests; higher ranks do."""
    admin = seeded_users["admin@acme.test"]
    operator_id = await _create(seeded_db, approval_tools, admin, tool="queue_note")
    admin_id = await _create(seeded_db, approval_tools, admin)
    viewer = await _headers(approvals_client, "viewer@acme.test", "viewer-pass")

    listed = await approvals_client.get(BASE, headers=viewer)
    assert listed.status_code == 200
    assert listed.json()["approvals"] == []
    for request_id in (operator_id, admin_id):
        response = await approvals_client.get(f"{BASE}/{request_id}", headers=viewer)
        assert response.status_code == 403
        assert response.json() == {"detail": "forbidden"}
    assert (await approvals_client.get(f"{BASE}/{uuid4()}", headers=viewer)).status_code == 404

    for headers in (await _admin(approvals_client), await _owner(approvals_client)):
        listed = await approvals_client.get(BASE, headers=headers)
        assert {item["id"] for item in listed.json()["approvals"]} == {
            str(operator_id),
            str(admin_id),
        }
        response = await approvals_client.get(f"{BASE}/{operator_id}", headers=headers)
        assert response.status_code == 200
        assert response.json()["required_role"] == "operator"


async def test_viewer_cannot_decide_or_execute(
    approvals_client: AsyncClient, seeded_db: Database, seeded_users, approval_tools
) -> None:
    """A role below the tool's requirement gets 403."""
    request_id = await _create(seeded_db, approval_tools, seeded_users["owner@acme.test"])
    headers = await _headers(approvals_client, "viewer@acme.test", "viewer-pass")

    for action in ("approve", "reject", "execute"):
        response = await approvals_client.post(f"{BASE}/{request_id}/{action}", headers=headers)
        assert response.status_code == 403, action


async def test_requester_approves_and_executes_exactly_once(
    approvals_client: AsyncClient, seeded_db: Database, seeded_users, approval_tools
) -> None:
    """Self-approval is allowed; execution runs once, a replay conflicts."""
    request_id = await _create(seeded_db, approval_tools, seeded_users["admin@acme.test"])
    headers = await _admin(approvals_client)

    approved = await approvals_client.post(f"{BASE}/{request_id}/approve", headers=headers)
    executed = await approvals_client.post(f"{BASE}/{request_id}/execute", headers=headers)
    replayed = await approvals_client.post(f"{BASE}/{request_id}/execute", headers=headers)

    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert executed.status_code == 200
    assert executed.json()["status"] == "executed"
    assert executed.json()["execution_result"] == {"delivered": True, "to": NOTE["to"]}
    assert replayed.status_code == 409
    assert len(approval_tools.spy.calls) == 1
    assert approval_tools.spy.calls[0][2].model_dump() == {**NOTE, "priority": 1}


async def test_second_decision_conflicts(
    approvals_client: AsyncClient, seeded_db: Database, seeded_users, approval_tools
) -> None:
    """A decided request answers 409 to any further decision."""
    request_id = await _create(seeded_db, approval_tools, seeded_users["owner@acme.test"])
    headers = await _owner(approvals_client)

    rejected = await approvals_client.post(
        f"{BASE}/{request_id}/reject", json={"reason": "wrong recipient"}, headers=headers
    )
    approve_again = await approvals_client.post(f"{BASE}/{request_id}/approve", headers=headers)
    reject_again = await approvals_client.post(f"{BASE}/{request_id}/reject", headers=headers)
    execute = await approvals_client.post(f"{BASE}/{request_id}/execute", headers=headers)

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["decision_reason"] == "wrong recipient"
    assert (approve_again.status_code, reject_again.status_code) == (409, 409)
    assert execute.status_code == 409
    assert approval_tools.spy.calls == []


async def test_expired_pending_request_is_gone(
    approvals_client: AsyncClient, seeded_db: Database, seeded_users, approval_tools
) -> None:
    """Deciding after the TTL answers 410 and leaves the request expired."""
    long_ago = datetime.now(UTC) - timedelta(seconds=TTL_SECONDS + 60)
    request_id = await _create(
        seeded_db, approval_tools, seeded_users["owner@acme.test"], now=long_ago
    )
    headers = await _owner(approvals_client)

    approve = await approvals_client.post(f"{BASE}/{request_id}/approve", headers=headers)
    fetched = await approvals_client.get(f"{BASE}/{request_id}", headers=headers)

    assert approve.status_code == 410
    assert fetched.json()["status"] == "expired"


async def test_expired_approval_cannot_execute(
    approvals_client: AsyncClient, seeded_db: Database, seeded_users, approval_tools
) -> None:
    """An approval granted in time still stops working at its TTL."""
    long_ago = datetime.now(UTC) - timedelta(seconds=TTL_SECONDS + 60)
    request_id = await _create(
        seeded_db,
        approval_tools,
        seeded_users["owner@acme.test"],
        now=long_ago,
        approve_at=long_ago + timedelta(seconds=1),
    )

    response = await approvals_client.post(
        f"{BASE}/{request_id}/execute", headers=await _owner(approvals_client)
    )

    assert response.status_code == 410
    assert approval_tools.spy.calls == []


# --- Per-request approver role and precheck (T11.a) ------------------------------


async def test_owner_tier_request_is_invisible_below_owner(
    approvals_client: AsyncClient, seeded_db: Database, seeded_users, tiered_tools
) -> None:
    """The stored per-request role drives the read filter, not the tool's invocation role."""
    request_id = await _create(
        seeded_db,
        tiered_tools,
        seeded_users["owner@acme.test"],
        tool="route_note",
        raw_input=OWNER_TIER_NOTE,
    )
    admin = await _admin(approvals_client)

    listed = await approvals_client.get(BASE, headers=admin)
    fetched = await approvals_client.get(f"{BASE}/{request_id}", headers=admin)
    approved = await approvals_client.post(f"{BASE}/{request_id}/approve", headers=admin)
    as_owner = await approvals_client.get(
        f"{BASE}/{request_id}", headers=await _owner(approvals_client)
    )

    assert listed.json()["approvals"] == []
    assert (fetched.status_code, approved.status_code) == (403, 403)
    assert as_owner.status_code == 200
    assert as_owner.json()["required_role"] == "owner"


async def test_tampered_approver_role_is_a_binding_conflict(
    approvals_client: AsyncClient, seeded_db: Database, seeded_users, tiered_tools
) -> None:
    """Lowering the stored role after approval answers 409 and burns the request."""
    request_id = await _create(
        seeded_db,
        tiered_tools,
        seeded_users["owner@acme.test"],
        approve_at=datetime.now(UTC),
        tool="route_note",
        raw_input=OWNER_TIER_NOTE,
    )
    async with seeded_db.sessionmaker() as session:
        await session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == request_id)
            .values(required_role="admin")
        )
        await session.commit()

    response = await approvals_client.post(
        f"{BASE}/{request_id}/execute", headers=await _admin(approvals_client)
    )
    fetched = await approvals_client.get(
        f"{BASE}/{request_id}", headers=await _owner(approvals_client)
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "approval binding mismatch"}
    assert fetched.json()["status"] == "failed"
    assert tiered_tools.spy.calls == []


async def test_precheck_refusal_at_execution_answers_failed_with_app_services(
    approvals_app: FastAPI,
    approvals_client: AsyncClient,
    seeded_db: Database,
    seeded_users,
    tiered_tools,
) -> None:
    """The route passes the app's model and storage; a refusal is a consumed, failed run."""
    request_id = await _create(
        seeded_db,
        tiered_tools,
        seeded_users["owner@acme.test"],
        approve_at=datetime.now(UTC),
        tool="route_note",
    )
    tiered_tools.precheck.refusal = "note already routed"
    headers = await _owner(approvals_client)

    executed = await approvals_client.post(f"{BASE}/{request_id}/execute", headers=headers)
    replayed = await approvals_client.post(f"{BASE}/{request_id}/execute", headers=headers)

    assert executed.status_code == 200
    assert executed.json()["status"] == "failed"
    assert executed.json()["execution_error"] == "precheck_failed: note already routed"
    assert replayed.status_code == 409
    assert tiered_tools.spy.calls == []
    [context] = tiered_tools.precheck.contexts
    assert context.llm is approvals_app.state.llm
    assert context.storage is approvals_app.state.storage
