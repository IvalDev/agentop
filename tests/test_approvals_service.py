"""Approvals service tests: exact binding, TTL, one-shot decisions, single use.

Every test drives a test-only ``send_note`` ACTION tool on a local registry
(see ``approval_tools`` in conftest) and an injected clock, so TTL behavior
is deterministic and no production tool is involved. Per-request approver
roles and prechecks use the test-only ``route_note`` (``tiered_tools``).
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from app.core.config import Settings
from app.core.db import Database
from app.modules.agent.llm import FakeLLMProvider
from app.modules.approvals.exceptions import (
    ApprovalBindingError,
    ApprovalExpiredError,
    ApprovalForbiddenError,
    ApprovalNotFoundError,
    ApprovalNotRequiredError,
    ApprovalStateError,
)
from app.modules.approvals.models import ApprovalRequest, ApprovalStatus
from app.modules.approvals.service import (
    approve_request,
    create_approval_request,
    execute_approved_action,
    get_approval_request,
    list_approval_requests,
    reject_request,
)
from app.modules.documents.storage import LocalDocumentStorage
from app.modules.identity.deps import CurrentUser
from app.modules.identity.models import UserRole
from app.modules.tools.exceptions import ToolApprovalRoleError
from app.modules.tools.executor import compute_action_key

T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
TTL_SECONDS = 3600
EXPIRY = T0 + timedelta(seconds=TTL_SECONDS)
SETTINGS = Settings(approval_ttl_seconds=TTL_SECONDS)
NOTE = {"to": "ops@acme.test", "body": "Quarterly report is ready"}
#: ``route_note`` inputs per approver tier (see ``note_approver`` in conftest).
TIER_NOTES = {
    "operator": {**NOTE, "priority": 1},
    "admin": {**NOTE, "priority": 3},
    "owner": {**NOTE, "priority": 5},
}

ADMIN = "admin@acme.test"
OWNER = "owner@acme.test"
VIEWER = "viewer@acme.test"
FOREIGN_OWNER = "owner@globex.test"


async def _create(
    db: Database, tools, user: CurrentUser, raw_input: dict = NOTE, *, tool: str = "send_note"
) -> UUID:
    async with db.sessionmaker() as session:
        request = await create_approval_request(
            session, tools.registry, user, tool, raw_input, settings=SETTINGS, now=T0
        )
        await session.commit()
        return request.id


async def _approve(db: Database, user: CurrentUser, request_id: UUID, now: datetime = T0):
    async with db.sessionmaker() as session:
        return await approve_request(session, user, request_id, now=now)


async def _reject(db: Database, user: CurrentUser, request_id: UUID, now: datetime = T0):
    async with db.sessionmaker() as session:
        return await reject_request(session, user, request_id, reason="not now", now=now)


async def _execute(db: Database, tools, user: CurrentUser, request_id: UUID, now: datetime = T0):
    async with db.sessionmaker() as session:
        return await execute_approved_action(session, tools.registry, user, request_id, now=now)


async def _reload(db: Database, user: CurrentUser, request_id: UUID) -> ApprovalRequest:
    async with db.sessionmaker() as session:
        return await get_approval_request(session, user, request_id)


async def test_create_binds_canonical_payload_to_computed_key(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """The stored payload is the validated input and the key is recomputed."""
    admin = seeded_users[ADMIN]
    async with seeded_db.sessionmaker() as session:
        request = await create_approval_request(
            session, approval_tools.registry, admin, "send_note", NOTE, settings=SETTINGS, now=T0
        )
        await session.commit()

    validated = approval_tools.registry.get("send_note").input_model.model_validate(NOTE)
    assert request.status is ApprovalStatus.PENDING
    assert request.tenant_id == admin.tenant_id
    assert request.requested_by == admin.id
    assert request.input_payload == {**NOTE, "priority": 1}
    assert request.action_key == compute_action_key("send_note", validated)
    assert request.required_role == "admin"
    assert (request.created_at, request.expires_at) == (T0, EXPIRY)
    assert approval_tools.spy.calls == []


async def test_create_refuses_tool_without_approval_policy(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """AUTO tools run directly; an approval request for them is a caller bug."""
    async with seeded_db.sessionmaker() as session:
        with pytest.raises(ApprovalNotRequiredError):
            await create_approval_request(
                session,
                approval_tools.registry,
                seeded_users[ADMIN],
                "peek_note",
                NOTE,
                settings=SETTINGS,
                now=T0,
            )


async def test_create_refuses_requester_below_tool_role(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """Only a user allowed to invoke the tool may ask for its approval."""
    async with seeded_db.sessionmaker() as session:
        with pytest.raises(ApprovalForbiddenError):
            await create_approval_request(
                session,
                approval_tools.registry,
                seeded_users[VIEWER],
                "send_note",
                NOTE,
                settings=SETTINGS,
                now=T0,
            )


async def test_requester_may_approve_own_request(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """Product policy: a requester with a sufficient role may self-approve."""
    admin = seeded_users[ADMIN]
    request_id = await _create(seeded_db, approval_tools, admin)

    request = await _approve(seeded_db, admin, request_id, now=T0 + timedelta(minutes=5))

    assert request.status is ApprovalStatus.APPROVED
    assert request.decided_by == admin.id
    assert request.decided_at == T0 + timedelta(minutes=5)


async def test_viewer_cannot_decide(seeded_db: Database, seeded_users, approval_tools) -> None:
    """A role below the tool's requirement gets forbidden; the request stays pending."""
    request_id = await _create(seeded_db, approval_tools, seeded_users[ADMIN])
    viewer = seeded_users[VIEWER]

    with pytest.raises(ApprovalForbiddenError):
        await _approve(seeded_db, viewer, request_id)
    with pytest.raises(ApprovalForbiddenError):
        await _reject(seeded_db, viewer, request_id)

    assert (await _reload(seeded_db, seeded_users[ADMIN], request_id)).status is (
        ApprovalStatus.PENDING
    )


async def test_superadmin_passes_role_check_in_own_tenant(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """``is_superadmin`` bypasses the role rank, mirroring ``require_roles``."""
    request_id = await _create(seeded_db, approval_tools, seeded_users[ADMIN])
    superviewer = replace(seeded_users[VIEWER], is_superadmin=True)

    request = await _approve(seeded_db, superviewer, request_id)

    assert request.status is ApprovalStatus.APPROVED


async def test_other_tenant_sees_nothing_even_as_superadmin(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """Cross-tenant ids behave exactly like missing ids for every operation."""
    request_id = await _create(seeded_db, approval_tools, seeded_users[ADMIN])
    foreign_owner = seeded_users[FOREIGN_OWNER]

    for outsider in (foreign_owner, replace(foreign_owner, is_superadmin=True)):
        async with seeded_db.sessionmaker() as session:
            assert await list_approval_requests(session, outsider) == []
            with pytest.raises(ApprovalNotFoundError):
                await get_approval_request(session, outsider, request_id)
        with pytest.raises(ApprovalNotFoundError):
            await _approve(seeded_db, outsider, request_id)
        with pytest.raises(ApprovalNotFoundError):
            await _reject(seeded_db, outsider, request_id)
        with pytest.raises(ApprovalNotFoundError):
            await _execute(seeded_db, approval_tools, outsider, request_id)

    assert (await _reload(seeded_db, seeded_users[ADMIN], request_id)).status is (
        ApprovalStatus.PENDING
    )


async def test_decisions_are_one_shot(seeded_db: Database, seeded_users, approval_tools) -> None:
    """Approved and rejected requests can never be re-decided."""
    owner = seeded_users[OWNER]
    approved_id = await _create(seeded_db, approval_tools, owner)
    rejected_id = await _create(seeded_db, approval_tools, owner)
    await _approve(seeded_db, owner, approved_id)
    await _reject(seeded_db, owner, rejected_id)

    for request_id in (approved_id, rejected_id):
        with pytest.raises(ApprovalStateError):
            await _approve(seeded_db, owner, request_id)
        with pytest.raises(ApprovalStateError):
            await _reject(seeded_db, owner, request_id)

    assert (await _reload(seeded_db, owner, approved_id)).status is ApprovalStatus.APPROVED
    assert (await _reload(seeded_db, owner, rejected_id)).status is ApprovalStatus.REJECTED


async def test_rejected_request_records_reason_and_never_executes(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """A rejection keeps its reason and blocks execution."""
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, approval_tools, owner)

    request = await _reject(seeded_db, owner, request_id)
    with pytest.raises(ApprovalStateError):
        await _execute(seeded_db, approval_tools, owner, request_id)

    assert request.status is ApprovalStatus.REJECTED
    assert request.decision_reason == "not now"
    assert approval_tools.spy.calls == []


@pytest.mark.parametrize("decide", [_approve, _reject], ids=["approve", "reject"])
async def test_decision_after_ttl_marks_request_expired(
    decide, seeded_db: Database, seeded_users, approval_tools
) -> None:
    """At or after ``expires_at`` a pending request is expired, not decided."""
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, approval_tools, owner)

    with pytest.raises(ApprovalExpiredError):
        await decide(seeded_db, owner, request_id, now=EXPIRY)
    with pytest.raises(ApprovalExpiredError):
        await _approve(seeded_db, owner, request_id, now=T0)

    request = await _reload(seeded_db, owner, request_id)
    assert request.status is ApprovalStatus.EXPIRED
    assert request.decided_by is None


async def test_execute_after_ttl_marks_request_expired_and_skips_handler(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """An approval stops authorizing anything once its TTL has elapsed."""
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, approval_tools, owner)
    await _approve(seeded_db, owner, request_id)

    with pytest.raises(ApprovalExpiredError):
        await _execute(seeded_db, approval_tools, owner, request_id, now=EXPIRY)

    assert (await _reload(seeded_db, owner, request_id)).status is ApprovalStatus.EXPIRED
    assert approval_tools.spy.calls == []


async def test_execute_requires_an_approved_request(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """A pending request cannot be executed."""
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, approval_tools, owner)

    with pytest.raises(ApprovalStateError):
        await _execute(seeded_db, approval_tools, owner, request_id)

    assert approval_tools.spy.calls == []


async def test_execute_requires_the_tool_role_without_burning_the_approval(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """A viewer cannot execute, and the refusal leaves the approval usable."""
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, approval_tools, owner)
    await _approve(seeded_db, owner, request_id)

    with pytest.raises(ApprovalForbiddenError):
        await _execute(seeded_db, approval_tools, seeded_users[VIEWER], request_id)

    assert (await _reload(seeded_db, owner, request_id)).status is ApprovalStatus.APPROVED
    assert approval_tools.spy.calls == []


async def test_execute_runs_the_stored_payload_exactly_once(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """Approve → execute runs the handler once with the stored payload only."""
    admin, owner = seeded_users[ADMIN], seeded_users[OWNER]
    request_id = await _create(seeded_db, approval_tools, admin)
    await _approve(seeded_db, owner, request_id)

    later = T0 + timedelta(seconds=1)
    outcome = await _execute(seeded_db, approval_tools, admin, request_id, now=later)
    with pytest.raises(ApprovalStateError):
        await _execute(seeded_db, approval_tools, admin, request_id)

    assert outcome.result.status == "ok"
    assert outcome.request.status is ApprovalStatus.EXECUTED
    assert outcome.request.executed_by == admin.id
    assert outcome.request.execution_result == {"delivered": True, "to": NOTE["to"]}
    assert len(approval_tools.spy.calls) == 1
    tenant_id, user, validated = approval_tools.spy.calls[0]
    assert (tenant_id, user.id) == (admin.tenant_id, admin.id)
    assert validated.model_dump() == {**NOTE, "priority": 1}
    stored = await _reload(seeded_db, owner, request_id)
    assert stored.status is ApprovalStatus.EXECUTED


async def test_concurrent_executes_run_the_handler_once(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """Racing executions: the conditional claim lets exactly one through."""
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, approval_tools, owner)
    await _approve(seeded_db, owner, request_id)

    outcomes = await asyncio.gather(
        *(_execute(seeded_db, approval_tools, owner, request_id) for _ in range(3)),
        return_exceptions=True,
    )

    succeeded = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, ApprovalStateError)]
    assert len(succeeded) == 1
    assert len(refused) == 2
    assert len(approval_tools.spy.calls) == 1


async def test_handler_failure_marks_request_failed_and_single_use(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """A failed execution is recorded and never retried with the same approval."""
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, approval_tools, owner)
    await _approve(seeded_db, owner, request_id)
    approval_tools.spy.error = RuntimeError("smtp down")

    outcome = await _execute(seeded_db, approval_tools, owner, request_id)
    with pytest.raises(ApprovalStateError):
        await _execute(seeded_db, approval_tools, owner, request_id)

    assert outcome.result.status == "error"
    assert outcome.request.status is ApprovalStatus.FAILED
    # Reviewers see the masked code; the handler's detail stays in the audit trail.
    assert outcome.request.execution_error == "tool_failed"
    assert len(approval_tools.spy.calls) == 1


@pytest.mark.parametrize(
    "tampered",
    [
        {"input_payload": {**NOTE, "to": "attacker@evil.test", "priority": 1}},
        {"input_payload": {**NOTE, "priority": 1, "bcc": "attacker@evil.test"}},
        {"action_key": "send_note:" + "0" * 64},
        {"tool_name": "peek_note"},
    ],
    ids=["payload-changed", "payload-extra-field", "key-changed", "tool-changed"],
)
async def test_binding_mismatch_is_refused_and_burns_the_approval(
    tampered: dict, seeded_db: Database, seeded_users, approval_tools
) -> None:
    """A row whose payload no longer matches its key never executes again."""
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, approval_tools, owner)
    await _approve(seeded_db, owner, request_id)
    async with seeded_db.sessionmaker() as session:
        await session.execute(
            update(ApprovalRequest).where(ApprovalRequest.id == request_id).values(**tampered)
        )
        await session.commit()

    with pytest.raises(ApprovalBindingError):
        await _execute(seeded_db, approval_tools, owner, request_id)
    with pytest.raises(ApprovalStateError):
        await _execute(seeded_db, approval_tools, owner, request_id)

    assert approval_tools.spy.calls == []
    assert (await _reload(seeded_db, owner, request_id)).status is ApprovalStatus.FAILED


async def test_list_is_tenant_scoped_and_filters_by_status(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """Listing returns the caller's tenant rows, optionally by status."""
    owner = seeded_users[OWNER]
    pending_id = await _create(seeded_db, approval_tools, owner)
    approved_id = await _create(seeded_db, approval_tools, owner)
    await _approve(seeded_db, owner, approved_id)

    async with seeded_db.sessionmaker() as session:
        everything = await list_approval_requests(session, seeded_users[ADMIN])
        pending = await list_approval_requests(session, owner, status=ApprovalStatus.PENDING)

    assert {request.id for request in everything} == {pending_id, approved_id}
    assert [request.id for request in pending] == [pending_id]


async def _visible(db: Database, user: CurrentUser, status: ApprovalStatus | None = None):
    async with db.sessionmaker() as session:
        return {request.id for request in await list_approval_requests(session, user, status)}


async def test_reads_require_the_tool_role(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """List hides and get forbids requests whose tool role the reader lacks."""
    admin, viewer = seeded_users[ADMIN], seeded_users[VIEWER]
    operator = replace(viewer, role=UserRole.OPERATOR)
    operator_id = await _create(seeded_db, approval_tools, admin, tool="queue_note")
    admin_id = await _create(seeded_db, approval_tools, admin)

    assert await _visible(seeded_db, viewer) == set()
    for request_id in (operator_id, admin_id):
        with pytest.raises(ApprovalForbiddenError):
            await _reload(seeded_db, viewer, request_id)

    assert await _visible(seeded_db, operator) == {operator_id}
    assert (await _reload(seeded_db, operator, operator_id)).required_role == "operator"
    with pytest.raises(ApprovalForbiddenError):
        await _reload(seeded_db, operator, admin_id)

    superviewer = replace(viewer, is_superadmin=True)
    for reader in (admin, seeded_users[OWNER], superviewer):
        assert await _visible(seeded_db, reader) == {operator_id, admin_id}
        assert (await _reload(seeded_db, reader, operator_id)).id == operator_id
        assert (await _reload(seeded_db, reader, admin_id)).id == admin_id


async def test_requester_reads_own_request_and_filters_keep_role_scope(
    seeded_db: Database, seeded_users, approval_tools
) -> None:
    """A self-requester at the tool's rank sees it; status filters stay role-scoped."""
    operator = replace(seeded_users[VIEWER], role=UserRole.OPERATOR)
    own_id = await _create(seeded_db, approval_tools, operator, tool="queue_note")
    hidden_id = await _create(seeded_db, approval_tools, seeded_users[ADMIN])
    await _approve(seeded_db, seeded_users[ADMIN], hidden_id)

    assert (await _reload(seeded_db, operator, own_id)).requested_by == operator.id
    assert await _visible(seeded_db, operator, ApprovalStatus.PENDING) == {own_id}
    assert await _visible(seeded_db, operator, ApprovalStatus.APPROVED) == set()


async def test_unknown_id_is_not_found_before_the_role_check(
    seeded_db: Database, seeded_users
) -> None:
    """An insufficient role never turns a missing id into forbidden."""
    with pytest.raises(ApprovalNotFoundError):
        await _reload(seeded_db, seeded_users[VIEWER], uuid4())


# --- Per-request approver role and precheck (T11.a) ------------------------------


async def test_create_stores_the_resolved_approver_role_and_reads_honor_it(
    seeded_db: Database, seeded_users, tiered_tools
) -> None:
    """The stored role follows the input's tier; the read filter and decisions use it."""
    operator = replace(seeded_users[VIEWER], role=UserRole.OPERATOR)
    admin, owner = seeded_users[ADMIN], seeded_users[OWNER]
    ids = {
        tier: await _create(seeded_db, tiered_tools, operator, note, tool="route_note")
        for tier, note in TIER_NOTES.items()
    }

    for tier, request_id in ids.items():
        assert (await _reload(seeded_db, owner, request_id)).required_role == tier
    assert await _visible(seeded_db, operator) == {ids["operator"]}
    for tier in ("admin", "owner"):
        with pytest.raises(ApprovalForbiddenError):
            await _reload(seeded_db, operator, ids[tier])
    assert await _visible(seeded_db, admin) == {ids["operator"], ids["admin"]}
    assert await _visible(seeded_db, owner) == set(ids.values())
    with pytest.raises(ApprovalForbiddenError):
        await _approve(seeded_db, admin, ids["owner"])
    assert (await _approve(seeded_db, owner, ids["owner"])).status is ApprovalStatus.APPROVED


async def test_create_with_a_failing_resolver_stores_nothing(
    seeded_db: Database, seeded_users, tiered_tools
) -> None:
    """A request is never persisted with a guessed approver role."""

    def broken(validated_input) -> UserRole:
        raise LookupError("tier table missing")

    spec = tiered_tools.registry.get("route_note")
    tiered_tools.registry.register(replace(spec, name="broken_note", approval_role=broken))

    with pytest.raises(ToolApprovalRoleError):
        await _create(seeded_db, tiered_tools, seeded_users[OWNER], tool="broken_note")

    async with seeded_db.sessionmaker() as session:
        assert (await session.execute(select(ApprovalRequest))).scalars().all() == []


@pytest.mark.parametrize(
    ("tier", "tampered_role", "executor"),
    [("owner", "admin", ADMIN), ("operator", "owner", OWNER)],
    ids=["role-lowered", "role-raised"],
)
async def test_execute_refuses_a_stored_role_that_no_longer_matches_the_input(
    tier: str,
    tampered_role: str,
    executor: str,
    seeded_db: Database,
    seeded_users,
    tiered_tools,
    audit_log,
) -> None:
    """The approver role re-derived from the payload must equal the stored one."""
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, tiered_tools, owner, TIER_NOTES[tier], tool="route_note")
    await _approve(seeded_db, owner, request_id)
    async with seeded_db.sessionmaker() as session:
        await session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == request_id)
            .values(required_role=tampered_role)
        )
        await session.commit()

    with pytest.raises(ApprovalBindingError):
        await _execute(seeded_db, tiered_tools, seeded_users[executor], request_id)
    with pytest.raises(ApprovalStateError):
        await _execute(seeded_db, tiered_tools, owner, request_id)

    assert tiered_tools.spy.calls == []
    assert tiered_tools.precheck.contexts == []
    assert (await _reload(seeded_db, owner, request_id)).status is ApprovalStatus.FAILED
    [mismatch] = await audit_log(seeded_db, action="approval.binding_mismatch")
    assert mismatch.resource_id == str(request_id)


async def test_precheck_refusal_at_execution_fails_the_request_once(
    seeded_db: Database, seeded_users, tiered_tools, audit_log
) -> None:
    """A refused approved call ends ``failed`` like a handler failure, never re-runnable."""
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, tiered_tools, owner, tool="route_note")
    await _approve(seeded_db, owner, request_id)
    tiered_tools.precheck.refusal = "note already routed"

    outcome = await _execute(seeded_db, tiered_tools, owner, request_id)
    with pytest.raises(ApprovalStateError):
        await _execute(seeded_db, tiered_tools, owner, request_id)

    assert (outcome.result.status, outcome.result.error_kind) == ("error", "precheck")
    assert outcome.request.status is ApprovalStatus.FAILED
    assert outcome.request.execution_error == "precheck_failed: note already routed"
    assert len(tiered_tools.precheck.contexts) == 1
    assert tiered_tools.spy.calls == []
    [refused] = await audit_log(seeded_db, action="tool.precheck_failed")
    assert refused.correlation_id == str(request_id)
    [failed] = await audit_log(seeded_db, action="approval.failed")
    assert failed.result == {"error": "precheck_failed: note already routed"}


async def test_approved_execution_gives_the_tool_the_llm_and_storage(
    seeded_db: Database, seeded_users, tiered_tools, tmp_path
) -> None:
    """The precheck and the handler of an approved call see the injected services."""
    llm, storage = FakeLLMProvider(), LocalDocumentStorage(tmp_path)
    owner = seeded_users[OWNER]
    request_id = await _create(seeded_db, tiered_tools, owner, tool="route_note")
    await _approve(seeded_db, owner, request_id)

    async with seeded_db.sessionmaker() as session:
        outcome = await execute_approved_action(
            session, tiered_tools.registry, owner, request_id, now=T0, llm=llm, storage=storage
        )

    assert outcome.request.status is ApprovalStatus.EXECUTED
    [precheck_context] = tiered_tools.precheck.contexts
    [handler_context] = tiered_tools.spy.contexts
    for context in (precheck_context, handler_context):
        assert context.llm is llm
        assert context.storage is storage
