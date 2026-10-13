"""Audit hook tests: every executor outcome, approval transition, and document step.

Executor and approval hooks run against the test-only tools of the
``approval_tools`` fixture (``send_note`` admin ACTION, ``peek_note`` viewer
AUTO extract) on the seeded two-tenant database.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import update

from app.core.config import Settings
from app.core.db import Database
from app.modules.approvals.exceptions import (
    ApprovalBindingError,
    ApprovalExpiredError,
    ApprovalForbiddenError,
)
from app.modules.approvals.grants import mint_grant
from app.modules.approvals.models import ApprovalRequest
from app.modules.approvals.service import (
    approve_request,
    create_approval_request,
    execute_approved_action,
    reject_request,
)
from app.modules.audit.models import ActorType
from app.modules.documents.extract import UnsupportedFormatError
from app.modules.documents.service import (
    FileTooLargeError,
    InvalidFilenameError,
    create_document,
    ingest_document,
)
from app.modules.documents.storage import LocalDocumentStorage
from app.modules.identity.models import UserRole
from app.modules.retrieval.embeddings import EmbeddingError
from app.modules.tools.executor import execute_tool
from app.modules.tools.registry import ApprovalPolicy, ToolKind, ToolSpec

T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
SETTINGS = Settings(approval_ttl_seconds=3600, max_upload_bytes=1000)
NOTE = {"to": "ops@acme.test", "body": "Quarterly report is ready"}
CANONICAL_NOTE = {**NOTE, "priority": 1}

OWNER = "owner@acme.test"
ADMIN = "admin@acme.test"
VIEWER = "viewer@acme.test"


def _stored(event) -> str:
    """Everything an event persisted about its call, as one searchable string."""
    return json.dumps([event.payload, event.result])


async def _run(db: Database, tools, user, tool: str, raw_input: dict, **options):
    async with db.sessionmaker() as session:
        result = await execute_tool(tools.registry, session, user, tool, raw_input, **options)
        await session.commit()
        return result


# --- Executor outcomes -------------------------------------------------------


async def test_successful_run_is_audited_once_with_canonical_payload(
    seeded_db: Database, seeded_users, approval_tools, audit_log
) -> None:
    """``tool.executed`` carries actor, tool, canonical input, and output."""
    viewer = seeded_users[VIEWER]

    result = await _run(seeded_db, approval_tools, viewer, "peek_note", NOTE)

    assert result.status == "ok"
    [event] = await audit_log(seeded_db)
    assert event.action == "tool.executed"
    assert event.tenant_id == viewer.tenant_id
    assert (event.actor_user_id, event.actor_type) == (viewer.id, ActorType.USER)
    assert event.tool_name == "peek_note"
    assert event.payload == CANONICAL_NOTE
    assert event.result == {"output": {"delivered": True, "to": "ops@acme.test"}}
    assert event.correlation_id is None


@pytest.mark.parametrize(
    ("email", "tool", "raw_input", "options", "action"),
    [
        (ADMIN, "ghost_tool", NOTE, {}, "tool.not_found"),
        (VIEWER, "send_note", NOTE, {}, "tool.denied"),
        (ADMIN, "peek_note", {**NOTE, "body": ""}, {}, "tool.invalid_input"),
        (ADMIN, "send_note", NOTE, {}, "tool.approval_required"),
        (ADMIN, "send_note", NOTE, {"dry_run": True}, "tool.dry_run"),
        (ADMIN, "send_note", NOTE, {"approval_grant": "forged"}, "tool.denied"),
    ],
    ids=["unknown-tool", "role", "invalid-input", "approval-required", "dry-run", "bad-grant"],
)
async def test_every_non_running_outcome_writes_exactly_one_event(
    seeded_db: Database,
    seeded_users,
    approval_tools,
    audit_log,
    email: str,
    tool: str,
    raw_input: dict,
    options: dict,
    action: str,
) -> None:
    """Refusals and no-op outcomes are audited; the handler never runs."""
    user = seeded_users[email]
    if options.get("approval_grant") == "forged":
        options = {"approval_grant": mint_grant(uuid4(), "send_note:" + "0" * 64)}

    await _run(seeded_db, approval_tools, user, tool, raw_input, **options)

    [event] = await audit_log(seeded_db)
    assert event.action == action
    assert (event.actor_user_id, event.actor_type) == (user.id, ActorType.USER)
    assert event.tool_name == tool
    assert approval_tools.spy.calls == []


async def test_approval_required_and_dry_run_record_the_validated_call(
    seeded_db: Database, seeded_users, approval_tools, audit_log
) -> None:
    """The would-be call is stored canonically with its approval facts."""
    admin = seeded_users[ADMIN]

    gated = await _run(seeded_db, approval_tools, admin, "send_note", NOTE)
    await _run(seeded_db, approval_tools, admin, "send_note", NOTE, dry_run=True)

    [required] = await audit_log(seeded_db, action="tool.approval_required")
    [dry_run] = await audit_log(seeded_db, action="tool.dry_run")
    assert required.payload == dry_run.payload == CANONICAL_NOTE
    assert required.result == {
        "action_key": gated.approval_request.action_key,
        "required_role": "admin",
    }
    assert dry_run.result == {"approval_required": True}


async def test_unvalidated_input_is_never_stored(
    seeded_db: Database, seeded_users, approval_tools, audit_log
) -> None:
    """Denied and invalid calls keep field names and error kinds, not values."""
    raw = {"to": "ops@acme.test", "body": "", "password": "hunter2"}

    await _run(seeded_db, approval_tools, seeded_users[VIEWER], "send_note", raw)
    await _run(seeded_db, approval_tools, seeded_users[ADMIN], "peek_note", raw)

    [denied] = await audit_log(seeded_db, action="tool.denied")
    [invalid] = await audit_log(seeded_db, action="tool.invalid_input")
    for event in (denied, invalid):
        assert event.payload == {"input_fields": ["body", "password", "to"]}
        assert "hunter2" not in _stored(event)
        assert "ops@acme.test" not in _stored(event)
    assert denied.result == {"error": "forbidden"}
    assert {(tuple(error["loc"]), error["type"]) for error in invalid.result["errors"]} == {
        (("body",), "string_too_short"),
        (("password",), "extra_forbidden"),
    }


async def test_handler_failure_is_audited_and_survives_the_callers_rollback(
    seeded_db: Database, seeded_users, approval_tools, audit_log
) -> None:
    """Failure and refusal events are written outside the caller's transaction."""
    admin = seeded_users[ADMIN]
    approval_tools.spy.error = RuntimeError("smtp down")

    async with seeded_db.sessionmaker() as session:
        for tool, raw_input, options in [
            ("peek_note", NOTE, {}),
            ("peek_note", {}, {}),
            ("send_note", NOTE, {}),
            ("send_note", NOTE, {"dry_run": True}),
        ]:
            await execute_tool(approval_tools.registry, session, admin, tool, raw_input, **options)
        await execute_tool(
            approval_tools.registry, session, seeded_users[VIEWER], "send_note", NOTE
        )
        await session.rollback()

    events = await audit_log(seeded_db)
    assert sorted(event.action for event in events) == [
        "tool.approval_required",
        "tool.denied",
        "tool.dry_run",
        "tool.failed",
        "tool.invalid_input",
    ]
    [failed] = await audit_log(seeded_db, action="tool.failed")
    assert failed.payload == CANONICAL_NOTE
    assert failed.result == {"error": "smtp down"}


async def test_invalid_output_is_audited_as_failed(
    seeded_db: Database, seeded_users, approval_tools, audit_log
) -> None:
    """Output violating ``output_model`` is a failure with a value-free summary."""

    class Delivery(BaseModel):
        model_config = ConfigDict(extra="forbid")
        receipt: str

    approval_tools.registry.register(
        ToolSpec(
            name="shaped_note",
            description="test-only extract whose output never matches",
            kind=ToolKind.EXTRACT,
            approval_policy=ApprovalPolicy.AUTO,
            required_role=UserRole.VIEWER,
            handler=approval_tools.spy,
            input_model=approval_tools.registry.get("peek_note").input_model,
            output_model=Delivery,
        )
    )

    result = await _run(seeded_db, approval_tools, seeded_users[ADMIN], "shaped_note", NOTE)

    assert result.status == "error"
    [event] = await audit_log(seeded_db)
    assert event.action == "tool.failed"
    assert event.result["error"] == "invalid tool output"
    assert "ops@acme.test" not in json.dumps(event.result)


async def test_agent_actor_and_correlation_are_recorded(
    seeded_db: Database, seeded_users, approval_tools, audit_log
) -> None:
    """T9's agent loop marks its calls as ``agent`` and threads a correlation id."""
    viewer = seeded_users[VIEWER]

    await _run(
        seeded_db,
        approval_tools,
        viewer,
        "peek_note",
        NOTE,
        actor_type=ActorType.AGENT,
        correlation_id="chat-42",
    )

    [event] = await audit_log(seeded_db)
    assert (event.actor_user_id, event.actor_type) == (viewer.id, ActorType.AGENT)
    assert event.correlation_id == "chat-42"


# --- Approval lifecycle ------------------------------------------------------


async def _create(db: Database, tools, user, *, now: datetime = T0, tool: str = "send_note"):
    async with db.sessionmaker() as session:
        request = await create_approval_request(
            session, tools.registry, user, tool, NOTE, settings=SETTINGS, now=now
        )
        await session.commit()
        return request.id


async def _approve(db: Database, user, request_id, now: datetime = T0):
    async with db.sessionmaker() as session:
        return await approve_request(session, user, request_id, now=now)


async def _execute(db: Database, tools, user, request_id, now: datetime = T0):
    async with db.sessionmaker() as session:
        return await execute_approved_action(session, tools.registry, user, request_id, now=now)


async def test_approved_execution_is_audited_and_correlated(
    seeded_db: Database, seeded_users, approval_tools, audit_log
) -> None:
    """created → approved → tool.executed → executed share the approval id."""
    admin, owner = seeded_users[ADMIN], seeded_users[OWNER]
    request_id = await _create(seeded_db, approval_tools, admin)
    await _approve(seeded_db, owner, request_id)
    await _execute(seeded_db, approval_tools, owner, request_id)

    events = {event.action: event for event in await audit_log(seeded_db)}
    assert sorted(events) == [
        "approval.approved",
        "approval.created",
        "approval.executed",
        "tool.executed",
    ]
    assert {event.correlation_id for event in events.values()} == {str(request_id)}
    created = events["approval.created"]
    assert created.actor_user_id == admin.id
    assert (created.resource_type, created.resource_id) == ("approval_request", str(request_id))
    assert created.tool_name == "send_note"
    assert created.payload == CANONICAL_NOTE
    assert created.result["action_key"].startswith("send_note:")
    for action in ("approval.approved", "tool.executed", "approval.executed"):
        assert events[action].actor_user_id == owner.id, action
    assert events["tool.executed"].payload == CANONICAL_NOTE


async def test_rejection_is_audited_with_its_reason(
    seeded_db: Database, seeded_users, approval_tools, audit_log
) -> None:
    """A rejection records who decided and why."""
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, approval_tools, owner)

    async with seeded_db.sessionmaker() as session:
        await reject_request(session, owner, request_id, reason="not now", now=T0)

    [event] = await audit_log(seeded_db, action="approval.rejected")
    assert event.actor_user_id == owner.id
    assert event.resource_id == str(request_id)
    assert event.payload == {"reason": "not now"}


async def test_expiry_is_audited_once_as_a_system_event(
    seeded_db: Database, seeded_users, approval_tools, audit_log
) -> None:
    """The TTL expires the request; repeated attempts do not re-audit it."""
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, approval_tools, owner)
    late = T0 + timedelta(hours=2)

    for _ in range(2):
        with pytest.raises(ApprovalExpiredError):
            await _approve(seeded_db, owner, request_id, now=late)

    [event] = await audit_log(seeded_db, action="approval.expired")
    assert (event.actor_user_id, event.actor_type) == (None, ActorType.SYSTEM)
    assert event.resource_id == str(request_id)
    assert event.payload["detected_by"] == owner.id


async def test_binding_mismatch_is_audited_without_running_the_tool(
    seeded_db: Database, seeded_users, approval_tools, audit_log
) -> None:
    """A tampered stored call is refused, marked, and audited."""
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, approval_tools, owner)
    await _approve(seeded_db, owner, request_id)
    async with seeded_db.sessionmaker() as session:
        await session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == request_id)
            .values(input_payload={**CANONICAL_NOTE, "to": "attacker@evil.test"})
        )
        await session.commit()

    with pytest.raises(ApprovalBindingError):
        await _execute(seeded_db, approval_tools, owner, request_id)

    [event] = await audit_log(seeded_db, action="approval.binding_mismatch")
    assert event.actor_user_id == owner.id
    assert event.correlation_id == str(request_id)
    assert await audit_log(seeded_db, action="tool.executed") == []


async def test_failed_execution_is_audited(
    seeded_db: Database, seeded_users, approval_tools, audit_log
) -> None:
    """A handler failure records both the tool failure and the approval outcome."""
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, approval_tools, owner)
    await _approve(seeded_db, owner, request_id)
    approval_tools.spy.error = RuntimeError("smtp down")

    await _execute(seeded_db, approval_tools, owner, request_id)

    [tool_failed] = await audit_log(seeded_db, action="tool.failed")
    [approval_failed] = await audit_log(seeded_db, action="approval.failed")
    assert tool_failed.correlation_id == approval_failed.correlation_id == str(request_id)
    assert approval_failed.result == {"error": "smtp down"}


async def test_denied_approval_operations_are_audited(
    seeded_db: Database, seeded_users, approval_tools, audit_log
) -> None:
    """Every refused create/approve/reject/execute leaves a durable trace."""
    owner, viewer = seeded_users[OWNER], seeded_users[VIEWER]
    request_id = await _create(seeded_db, approval_tools, owner)

    with pytest.raises(ApprovalForbiddenError):
        await _create(seeded_db, approval_tools, viewer)
    with pytest.raises(ApprovalForbiddenError):
        await _approve(seeded_db, viewer, request_id)
    async with seeded_db.sessionmaker() as session:
        with pytest.raises(ApprovalForbiddenError):
            await reject_request(session, viewer, request_id, now=T0)
    await _approve(seeded_db, owner, request_id)
    with pytest.raises(ApprovalForbiddenError):
        await _execute(seeded_db, approval_tools, viewer, request_id)

    denied = await audit_log(seeded_db, action="approval.denied")
    assert sorted(event.payload["operation"] for event in denied) == [
        "approve",
        "create",
        "execute",
        "reject",
    ]
    assert {event.actor_user_id for event in denied} == {viewer.id}
    assert {event.result["error"] for event in denied} == {"forbidden"}


# --- Documents ---------------------------------------------------------------


@pytest.fixture
def storage(tmp_path) -> LocalDocumentStorage:
    return LocalDocumentStorage(tmp_path / "storage")


async def _upload(db: Database, storage, user, *, content_type: str = "text/plain"):
    async with db.sessionmaker() as session:
        document = await create_document(
            session,
            user.tenant_id,
            "notes.txt",
            content_type,
            b"plain content for the audit trail",
            storage,
            settings=SETTINGS,
            actor_user_id=user.id,
        )
        await session.commit()
        return document.id


async def test_upload_and_ingest_are_audited_with_the_actor(
    seeded_db: Database, seeded_users, storage, audit_log
) -> None:
    """Upload and ingest record who acted on which document."""
    owner = seeded_users[OWNER]
    document_id = await _upload(seeded_db, storage, owner)
    async with seeded_db.sessionmaker() as session:
        await ingest_document(
            session, owner.tenant_id, document_id, storage, actor_user_id=owner.id
        )
        await session.commit()

    [uploaded] = await audit_log(seeded_db, action="document.uploaded")
    [ingested] = await audit_log(seeded_db, action="document.ingested")
    for event in (uploaded, ingested):
        assert (event.actor_user_id, event.actor_type) == (owner.id, ActorType.USER)
        assert (event.resource_type, event.resource_id) == ("document", str(document_id))
        assert event.tenant_id == owner.tenant_id
    assert uploaded.payload == {
        "filename": "notes.txt",
        "content_type": "text/plain",
        "size_bytes": 33,
    }
    assert ingested.result == {"chunks": 1, "embedded": False}


async def test_upload_event_rolls_back_with_the_upload(
    seeded_db: Database, seeded_users, storage, audit_log
) -> None:
    """A success event is atomic with its row: no document, no event."""
    owner = seeded_users[OWNER]
    async with seeded_db.sessionmaker() as session:
        await create_document(
            session,
            owner.tenant_id,
            "notes.txt",
            "text/plain",
            b"never committed",
            storage,
            settings=SETTINGS,
            actor_user_id=owner.id,
        )
        await session.rollback()

    assert await audit_log(seeded_db) == []


@pytest.mark.parametrize(
    ("filename", "data", "error"),
    [("../etc/passwd", b"x", InvalidFilenameError), ("big.txt", b"x" * 1001, FileTooLargeError)],
    ids=["traversal", "too-large"],
)
async def test_rejected_upload_is_audited_durably(
    seeded_db: Database, seeded_users, storage, audit_log, filename, data, error
) -> None:
    """A refused upload is recorded even though the request fails."""
    operator = replace(seeded_users[VIEWER], role=UserRole.OPERATOR)
    async with seeded_db.sessionmaker() as session:
        with pytest.raises(error):
            await create_document(
                session,
                operator.tenant_id,
                filename,
                "text/plain",
                data,
                storage,
                settings=SETTINGS,
                actor_user_id=operator.id,
            )
        await session.rollback()

    [event] = await audit_log(seeded_db, action="document.upload_rejected")
    assert event.actor_user_id == operator.id
    assert event.payload == {
        "filename": filename,
        "content_type": "text/plain",
        "size_bytes": len(data),
    }
    assert event.result["error"]


async def test_ingest_failure_is_audited_and_survives_the_callers_rollback(
    seeded_db: Database, seeded_users, storage, audit_log
) -> None:
    """The failure event commits with the service's own ``failed`` status."""
    owner = seeded_users[OWNER]
    document_id = await _upload(seeded_db, storage, owner, content_type="application/x-unknown")

    async with seeded_db.sessionmaker() as session:
        with pytest.raises(UnsupportedFormatError):
            await ingest_document(
                session, owner.tenant_id, document_id, storage, actor_user_id=owner.id
            )
        await session.rollback()

    [event] = await audit_log(seeded_db, action="document.ingest_failed")
    assert event.actor_user_id == owner.id
    assert event.resource_id == str(document_id)
    assert event.result["stage"] == "extraction"
    assert "unsupported content type" in event.result["error"]


async def test_embedding_failure_is_audited(
    seeded_db: Database, seeded_users, storage, audit_log
) -> None:
    """An embedding backend failure is an ``embedding``-stage ingest failure."""

    class DownEmbeddings:
        dimension = 8

        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingError("embedding backend down")

    owner = seeded_users[OWNER]
    document_id = await _upload(seeded_db, storage, owner)

    async with seeded_db.sessionmaker() as session:
        with pytest.raises(EmbeddingError):
            await ingest_document(
                session,
                owner.tenant_id,
                document_id,
                storage,
                embeddings=DownEmbeddings(),
                actor_user_id=owner.id,
            )

    [event] = await audit_log(seeded_db, action="document.ingest_failed")
    assert event.result == {"stage": "embedding", "error": "embedding backend down"}
    assert await audit_log(seeded_db, action="document.ingested") == []


async def test_worker_ingest_without_actor_is_a_system_event(
    seeded_db: Database, seeded_users, storage, audit_log
) -> None:
    """The ingest worker acts as the system (no user id)."""
    owner = seeded_users[OWNER]
    document_id = await _upload(seeded_db, storage, owner)
    async with seeded_db.sessionmaker() as session:
        await ingest_document(session, owner.tenant_id, document_id, storage)
        await session.commit()

    [event] = await audit_log(seeded_db, action="document.ingested")
    assert (event.actor_user_id, event.actor_type) == (None, ActorType.SYSTEM)
