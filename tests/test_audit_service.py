"""Audit service tests: append-only guards, durability, payload hygiene, queries.

Audit rows carry no foreign keys, so these tests write events for arbitrary
tenant ids on the empty ``identity_db`` schema.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, update

from app.core.config import Settings
from app.core.db import Database
from app.modules.audit.models import ActorType, AuditEvent, AuditImmutableError
from app.modules.audit.service import (
    REDACTED,
    AuditFilters,
    InvalidCursorError,
    configure_audit,
    list_events,
    persist_event,
    stage_event,
)
from app.modules.identity.deps import TenantContext
from app.modules.identity.models import UserRole

T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
TENANT = 1
OTHER_TENANT = 2
CONTEXT = TenantContext(tenant_id=TENANT, user_id=1, role=UserRole.OWNER)


@pytest.fixture
def small_audit_cap():
    """Shrink the payload cap to 256 bytes for one test, then restore it."""
    configure_audit(Settings(audit_max_payload_bytes=256))
    yield 256
    configure_audit(Settings())


async def _persist(db: Database, **fields) -> AuditEvent:
    fields.setdefault("tenant_id", TENANT)
    fields.setdefault("action", "tool.executed")
    async with db.sessionmaker() as session:
        return await persist_event(session, **fields)


async def test_staged_event_commits_with_the_caller_and_vanishes_on_rollback(
    identity_db: Database, audit_log
) -> None:
    """``stage_event`` joins the caller's transaction: atomic with its outcome."""
    async with identity_db.sessionmaker() as session:
        stage_event(session, tenant_id=TENANT, action="document.uploaded", actor_user_id=7)
        await session.rollback()
    assert await audit_log(identity_db) == []

    async with identity_db.sessionmaker() as session:
        stage_event(
            session,
            tenant_id=TENANT,
            action="document.uploaded",
            actor_user_id=7,
            resource_type="document",
            resource_id="doc-1",
            payload={"filename": "notes.txt"},
            result={"status": "uploaded"},
            correlation_id="req-1",
        )
        await session.commit()

    [event] = await audit_log(identity_db)
    assert event.tenant_id == TENANT
    assert event.action == "document.uploaded"
    assert (event.actor_user_id, event.actor_type) == (7, ActorType.USER)
    assert (event.resource_type, event.resource_id) == ("document", "doc-1")
    assert event.payload == {"filename": "notes.txt"}
    assert event.result == {"status": "uploaded"}
    assert event.correlation_id == "req-1"
    assert event.created_at.tzinfo is UTC


async def test_persisted_event_survives_the_callers_rollback(
    identity_db: Database, audit_log
) -> None:
    """``persist_event`` commits in its own transaction; a later rollback keeps it."""
    async with identity_db.sessionmaker() as session:
        stage_event(session, tenant_id=TENANT, action="tool.executed")
        await persist_event(session, tenant_id=TENANT, action="tool.denied", tool_name="send")
        await session.rollback()

    assert [event.action for event in await audit_log(identity_db)] == ["tool.denied"]


async def test_actor_defaults_to_system_without_a_user(identity_db: Database, audit_log) -> None:
    """No actor id means the system acted; an explicit type is kept."""
    await _persist(identity_db, action="approval.expired")
    await _persist(identity_db, action="tool.executed", actor_user_id=7, actor_type=ActorType.AGENT)

    system, agent = await audit_log(identity_db)
    assert (system.actor_user_id, system.actor_type) == (None, ActorType.SYSTEM)
    assert (agent.actor_user_id, agent.actor_type) == (7, ActorType.AGENT)


async def test_events_cannot_be_updated(identity_db: Database, audit_log) -> None:
    """Unit-of-work and bulk updates of an audit row are both refused."""
    event = await _persist(identity_db, payload={"value": 1})

    async with identity_db.sessionmaker() as session:
        stored = await session.get(AuditEvent, event.id)
        stored.action = "tool.denied"
        with pytest.raises(AuditImmutableError):
            await session.commit()
    async with identity_db.sessionmaker() as session:
        with pytest.raises(AuditImmutableError):
            await session.execute(update(AuditEvent).values(action="tool.denied"))
        with pytest.raises(AuditImmutableError):
            await session.execute(update(AuditEvent.__table__).values(payload={}))

    [unchanged] = await audit_log(identity_db)
    assert (unchanged.action, unchanged.payload) == ("tool.executed", {"value": 1})


async def test_events_cannot_be_deleted(identity_db: Database, audit_log) -> None:
    """Unit-of-work and bulk deletes of an audit row are both refused."""
    event = await _persist(identity_db)

    async with identity_db.sessionmaker() as session:
        await session.delete(await session.get(AuditEvent, event.id))
        with pytest.raises(AuditImmutableError):
            await session.commit()
    async with identity_db.sessionmaker() as session:
        with pytest.raises(AuditImmutableError):
            await session.execute(delete(AuditEvent))
        with pytest.raises(AuditImmutableError):
            await session.execute(delete(AuditEvent.__table__))

    assert [stored.id for stored in await audit_log(identity_db)] == [event.id]


async def test_secrets_are_redacted_at_any_depth(identity_db: Database, audit_log) -> None:
    """Secret-looking keys and bearer tokens never reach the audit table."""
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI3In0.c2lnbmF0dXJl"
    await _persist(
        identity_db,
        payload={
            "to": "ops@acme.test",
            "password": "hunter2",
            "nested": {"apiKey": "k-123", "items": [{"access_token": "t-456"}]},
            "note": f"forwarded header Bearer {jwt}",
        },
        result={"approval_grant": "g-789", "client_secret": "s-000", "ok": True},
    )

    [event] = await audit_log(identity_db)
    stored = json.dumps([event.payload, event.result])
    for secret in ("hunter2", "k-123", "t-456", jwt, "g-789", "s-000"):
        assert secret not in stored
    assert event.payload["password"] == REDACTED
    assert event.payload["nested"]["items"][0]["access_token"] == REDACTED
    assert event.payload["to"] == "ops@acme.test"
    assert event.payload["note"].startswith("forwarded header Bearer ")
    assert event.result["ok"] is True


async def test_oversized_payload_and_result_are_truncated_and_marked(
    identity_db: Database, audit_log, small_audit_cap: int
) -> None:
    """Above the configured cap a JSON value becomes a marked, bounded preview."""
    big = {"body": "x" * 1000}
    await _persist(identity_db, payload=big, result=big)
    await _persist(identity_db, action="tool.denied", payload={"body": "short"})

    truncated, small = await audit_log(identity_db)
    original_bytes = len(json.dumps(big, separators=(",", ":")).encode())
    for value in (truncated.payload, truncated.result):
        assert value["truncated"] is True
        assert value["original_bytes"] == original_bytes
        assert len(value["preview"].encode()) <= small_audit_cap
        assert value["preview"].startswith('{"body":"xxx')
    assert small.payload == {"body": "short"}


@pytest.mark.parametrize("action", ["Tool Executed", "tool", "tool.", "tool.Executed", ""])
async def test_action_must_be_a_dotted_lowercase_verb(identity_db: Database, action: str) -> None:
    """A malformed action is a programming error, rejected before any write."""
    async with identity_db.sessionmaker() as session:
        with pytest.raises(ValueError):
            stage_event(session, tenant_id=TENANT, action=action)


async def test_list_events_is_tenant_scoped_newest_first_and_paginated(
    identity_db: Database,
) -> None:
    """Keyset pages never repeat or skip rows, even with equal timestamps."""
    for index in range(5):
        await _persist(identity_db, now=T0 + timedelta(minutes=index), payload={"n": index})
    await _persist(identity_db, now=T0 + timedelta(minutes=2), payload={"n": 99})
    await _persist(identity_db, tenant_id=OTHER_TENANT, now=T0)

    seen: list[int] = []
    cursor = None
    async with identity_db.sessionmaker() as session:
        while True:
            page, cursor = await list_events(
                session, CONTEXT, AuditFilters(), limit=2, cursor=cursor
            )
            seen.extend(event.payload["n"] for event in page)
            if cursor is None:
                break

    assert sorted(seen) == [0, 1, 2, 3, 4, 99]
    assert seen[:2] == [4, 3]
    assert seen[-2:] == [1, 0]


async def test_list_events_prefix_filter_treats_wildcards_literally(
    identity_db: Database,
) -> None:
    """``action_prefix`` matches a literal prefix (``_``/``%`` are not wildcards)."""
    await _persist(identity_db, action="tool.executed")
    await _persist(identity_db, action="tool.denied")
    await _persist(identity_db, action="approval.created")

    async with identity_db.sessionmaker() as session:
        tools, _ = await list_events(session, CONTEXT, AuditFilters(action_prefix="tool."))
        wildcard, _ = await list_events(session, CONTEXT, AuditFilters(action_prefix="too_"))

    assert {event.action for event in tools} == {"tool.executed", "tool.denied"}
    assert wildcard == []


@pytest.mark.parametrize("cursor", ["not-base64!", "e30", "eyJ0IjoxfQ"])
async def test_list_events_rejects_malformed_cursors(identity_db: Database, cursor: str) -> None:
    """A tampered or foreign cursor is a client error, never a crash."""
    async with identity_db.sessionmaker() as session:
        with pytest.raises(InvalidCursorError):
            await list_events(session, CONTEXT, AuditFilters(), cursor=cursor)
