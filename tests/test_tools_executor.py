"""Tool executor tests: authorization, validation, approval gate, dry-run, errors.

Pure executor coverage uses a local registry with spy handlers so no test
depends on the built-in tools' database queries. Builtin wiring tests live at
the bottom of this file.
"""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.db import Database
from app.modules.agent.llm import FakeLLMProvider
from app.modules.approvals.grants import ApprovalGrant, mint_grant
from app.modules.documents.models import Document, DocumentChunk, DocumentStatus
from app.modules.documents.storage import LocalDocumentStorage
from app.modules.identity.deps import CurrentUser
from app.modules.identity.models import Tenant, UserRole
from app.modules.retrieval.embeddings import EmbeddingError, FakeEmbeddingProvider
from app.modules.tools import builtin
from app.modules.tools.exceptions import (
    ToolApprovalRoleError,
    ToolPrecheckError,
    ToolRefusalError,
)
from app.modules.tools.executor import compute_action_key, execute_tool, resolve_approval_role
from app.modules.tools.registry import (
    ApprovalPolicy,
    ToolContext,
    ToolKind,
    ToolRegistry,
    ToolSpec,
    default_registry,
)


class DemoInput(BaseModel):
    """Typed input: rejects ``value`` below 1 so validation can be exercised."""

    value: int = Field(ge=1)


class DemoOutput(BaseModel):
    """Structured output schema used to validate handler returns."""

    doubled: int


class SpyHandler:
    """Async handler double: records calls, returns a preset result or raises."""

    def __init__(self, result: object, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[int, CurrentUser, DemoInput]] = []
        self.contexts: list[ToolContext] = []

    async def __call__(self, context: ToolContext, validated_input: DemoInput) -> object:
        self.calls.append((context.tenant_id, context.user, validated_input))
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class ToolBundle:
    """Fresh registry plus the spies registered in it."""

    registry: ToolRegistry
    extract_spy: SpyHandler
    admin_spy: SpyHandler
    action_spy: SpyHandler
    bad_output_spy: SpyHandler
    boom_spy: SpyHandler


@pytest.fixture
def tools() -> ToolBundle:
    """Registry with one tool per executor path (role, approval, output, error)."""
    registry = ToolRegistry()
    extract_spy = SpyHandler(DemoOutput(doubled=4))
    admin_spy = SpyHandler(DemoOutput(doubled=2))
    action_spy = SpyHandler({"sent": True})
    bad_output_spy = SpyHandler({"wrong": True})
    boom_spy = SpyHandler(None, error=ValueError("kapow"))

    registry.register(
        ToolSpec(
            name="spy_extract",
            description="auto extract spy",
            kind=ToolKind.EXTRACT,
            approval_policy=ApprovalPolicy.AUTO,
            required_role=UserRole.OPERATOR,
            handler=extract_spy,
            input_model=DemoInput,
            output_model=DemoOutput,
        )
    )
    registry.register(
        ToolSpec(
            name="admin_extract",
            description="admin-only extract spy",
            kind=ToolKind.EXTRACT,
            approval_policy=ApprovalPolicy.AUTO,
            required_role=UserRole.ADMIN,
            handler=admin_spy,
            input_model=DemoInput,
            output_model=DemoOutput,
        )
    )
    registry.register(
        ToolSpec(
            name="demo_action",
            description="action spy",
            kind=ToolKind.ACTION,
            approval_policy=ApprovalPolicy.REQUIRE_APPROVAL,
            required_role=UserRole.ADMIN,
            handler=action_spy,
            input_model=DemoInput,
        )
    )
    registry.register(
        ToolSpec(
            name="bad_output",
            description="returns output violating its schema",
            kind=ToolKind.EXTRACT,
            approval_policy=ApprovalPolicy.AUTO,
            required_role=UserRole.VIEWER,
            handler=bad_output_spy,
            input_model=DemoInput,
            output_model=DemoOutput,
        )
    )
    registry.register(
        ToolSpec(
            name="boom",
            description="handler raises",
            kind=ToolKind.EXTRACT,
            approval_policy=ApprovalPolicy.AUTO,
            required_role=UserRole.VIEWER,
            handler=boom_spy,
            input_model=DemoInput,
        )
    )
    return ToolBundle(
        registry=registry,
        extract_spy=extract_spy,
        admin_spy=admin_spy,
        action_spy=action_spy,
        bad_output_spy=bad_output_spy,
        boom_spy=boom_spy,
    )


def make_user(
    role: UserRole = UserRole.OWNER,
    *,
    tenant_id: int = 1,
    is_superadmin: bool = False,
) -> CurrentUser:
    """Request-scoped principal used as the executor's ``user`` argument."""
    return CurrentUser(
        id=7,
        tenant_id=tenant_id,
        email=f"{role.value}@acme.test",
        full_name="Test User",
        role=role,
        is_superadmin=is_superadmin,
    )


async def test_auto_extract_executes_and_returns_ok(
    tools: ToolBundle, identity_db: Database
) -> None:
    """An AUTO extract runs its handler and returns validated output."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            tools.registry, session, make_user(), "spy_extract", {"value": 2}
        )

    assert result.status == "ok"
    assert result.output == DemoOutput(doubled=4)
    assert result.error is None
    assert len(tools.extract_spy.calls) == 1
    tenant_id, user, validated = tools.extract_spy.calls[0]
    assert tenant_id == 1
    assert user.role is UserRole.OWNER
    assert validated == DemoInput(value=2)


async def test_handler_receives_a_context_with_session_and_services(
    tools: ToolBundle, identity_db: Database, tmp_path: Path
) -> None:
    """The ``ToolContext`` carries the caller's session, principal, and providers."""
    embeddings = FakeEmbeddingProvider(dimension=8)
    llm, storage = FakeLLMProvider(), LocalDocumentStorage(tmp_path)
    async with identity_db.sessionmaker() as session:
        await execute_tool(
            tools.registry,
            session,
            make_user(tenant_id=3),
            "spy_extract",
            {"value": 2},
            correlation_id="run-1",
            embeddings=embeddings,
            llm=llm,
            storage=storage,
            timezone="Europe/Madrid",
        )
        await execute_tool(tools.registry, session, make_user(), "spy_extract", {"value": 2})
        context, bare = tools.extract_spy.contexts
        assert context.session is session

    assert (context.tenant_id, context.user.tenant_id) == (3, 3)
    assert context.embeddings is embeddings
    assert context.correlation_id == "run-1"
    assert context.llm is llm
    assert context.storage is storage
    assert context.timezone == "Europe/Madrid"
    # Services are optional: a caller that wires none gives handlers None.
    assert (bare.embeddings, bare.llm, bare.storage, bare.timezone) == (None, None, None, None)


async def test_invalid_input_returns_validation_error_and_skips_handler(
    tools: ToolBundle, identity_db: Database
) -> None:
    """Input failing the schema yields a validation error, handler untouched."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            tools.registry, session, make_user(), "spy_extract", {"value": 0}
        )

    assert result.status == "error"
    assert result.error_kind == "validation"
    assert "value" in (result.error or "")
    assert tools.extract_spy.calls == []


async def test_viewer_invoking_operator_tool_is_forbidden(
    tools: ToolBundle, identity_db: Database
) -> None:
    """A viewer below the required role gets a generic forbidden error."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            tools.registry,
            session,
            make_user(UserRole.VIEWER),
            "spy_extract",
            {"value": 2},
        )

    assert result.status == "error"
    assert result.error_kind == "forbidden"
    assert result.error == "forbidden"
    # The message must not reveal which role the tool demands.
    assert "operator" not in (result.error or "")
    assert tools.extract_spy.calls == []


async def test_owner_may_invoke_admin_tool(tools: ToolBundle, identity_db: Database) -> None:
    """The role rank map lets owner (above admin) run an ADMIN tool."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            tools.registry, session, make_user(UserRole.OWNER), "admin_extract", {"value": 1}
        )

    assert result.status == "ok"
    assert len(tools.admin_spy.calls) == 1


async def test_higher_role_passes_lower_requirement(
    tools: ToolBundle, identity_db: Database
) -> None:
    """ADMIN outranks a VIEWER-gated tool, so the rank map lets it through."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            tools.registry, session, make_user(UserRole.ADMIN), "bad_output", {"value": 1}
        )

    # The handler ran (its invalid return is asserted by the dedicated test);
    # reaching it at all proves the role check passed for admin.
    assert len(tools.bad_output_spy.calls) == 1
    assert result.error_kind == "execution"


async def test_superadmin_bypasses_role_rank(tools: ToolBundle, identity_db: Database) -> None:
    """``is_superadmin`` bypasses the rank check, mirroring ``require_roles``."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            tools.registry,
            session,
            make_user(UserRole.VIEWER, is_superadmin=True),
            "spy_extract",
            {"value": 2},
        )

    assert result.status == "ok"


async def test_dry_run_still_requires_sufficient_role(
    tools: ToolBundle, identity_db: Database
) -> None:
    """Authorization runs before the dry-run short circuit."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            tools.registry,
            session,
            make_user(UserRole.VIEWER),
            "spy_extract",
            {"value": 2},
            dry_run=True,
        )

    assert result.status == "error"
    assert result.error_kind == "forbidden"
    assert tools.extract_spy.calls == []


async def test_require_approval_without_grant_returns_approval_request(
    tools: ToolBundle, identity_db: Database
) -> None:
    """An unapproved ACTION returns approval_required without calling the handler."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            tools.registry, session, make_user(), "demo_action", {"value": 3}
        )

    assert result.status == "approval_required"
    assert tools.action_spy.calls == []
    payload = result.approval_request
    assert payload.tool_name == "demo_action"
    assert payload.input == {"value": 3}
    assert payload.required_role == "admin"
    assert payload.action_key.startswith("demo_action:")


async def test_action_key_is_stable_and_bound_to_input(
    tools: ToolBundle, identity_db: Database
) -> None:
    """Same tool+input → same key; a changed input → a different key."""
    async with identity_db.sessionmaker() as session:
        first = await execute_tool(
            tools.registry, session, make_user(), "demo_action", {"value": 3}
        )
        again = await execute_tool(
            tools.registry, session, make_user(), "demo_action", {"value": 3}
        )
        other = await execute_tool(
            tools.registry, session, make_user(), "demo_action", {"value": 4}
        )

    assert first.approval_request.action_key == again.approval_request.action_key
    assert first.approval_request.action_key != other.approval_request.action_key
    # The helper computes the same key the approval payload carries.
    assert compute_action_key("demo_action", DemoInput(value=3)) == (
        first.approval_request.action_key
    )


async def test_minted_grant_for_the_exact_action_runs_handler_once(
    tools: ToolBundle, identity_db: Database
) -> None:
    """A live grant bound to this tool+input runs the handler, then is spent."""
    grant = mint_grant(uuid4(), compute_action_key("demo_action", DemoInput(value=3)))
    async with identity_db.sessionmaker() as session:
        first = await execute_tool(
            tools.registry, session, make_user(), "demo_action", {"value": 3}, approval_grant=grant
        )
        replayed = await execute_tool(
            tools.registry, session, make_user(), "demo_action", {"value": 3}, approval_grant=grant
        )

    assert first.status == "ok"
    assert (replayed.status, replayed.error_kind) == ("error", "forbidden")
    assert len(tools.action_spy.calls) == 1


async def test_grant_for_another_input_is_refused(tools: ToolBundle, identity_db: Database) -> None:
    """A grant minted for one payload cannot authorize a different one."""
    grant = mint_grant(uuid4(), compute_action_key("demo_action", DemoInput(value=3)))
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            tools.registry, session, make_user(), "demo_action", {"value": 4}, approval_grant=grant
        )

    assert (result.status, result.error_kind) == ("error", "forbidden")
    assert tools.action_spy.calls == []


@pytest.mark.parametrize(
    "forged",
    [
        "t7-issued-placeholder",
        ApprovalGrant(uuid4(), compute_action_key("demo_action", DemoInput(value=3))),
    ],
    ids=["free-form-string", "unminted-grant"],
)
async def test_forged_grants_are_refused(
    forged: object, tools: ToolBundle, identity_db: Database
) -> None:
    """Only grants minted by the approvals service unlock an ACTION tool."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            tools.registry,
            session,
            make_user(),
            "demo_action",
            {"value": 3},
            approval_grant=forged,
        )

    assert (result.status, result.error_kind) == ("error", "forbidden")
    assert result.approval_request is None
    assert tools.action_spy.calls == []


async def test_dry_run_reports_would_be_action_without_handler(
    tools: ToolBundle, identity_db: Database
) -> None:
    """Dry-run of an ACTION describes the call and skips both gate and handler."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            tools.registry,
            session,
            make_user(),
            "demo_action",
            {"value": 3},
            dry_run=True,
        )

    assert result.status == "dry_run"
    assert tools.action_spy.calls == []
    info = result.output
    assert info.tool_name == "demo_action"
    assert info.kind == "action"
    assert info.approval_policy == "require_approval"
    assert info.validated_input == {"value": 3}
    assert info.approval_required is True


async def test_dry_run_of_auto_tool_needs_no_approval(
    tools: ToolBundle, identity_db: Database
) -> None:
    """Dry-run of an AUTO extract reports approval_required False."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            tools.registry,
            session,
            make_user(),
            "spy_extract",
            {"value": 2},
            dry_run=True,
        )

    assert result.status == "dry_run"
    assert result.output.approval_required is False
    assert tools.extract_spy.calls == []


async def test_handler_exception_is_masked_for_callers_and_kept_in_audit(
    tools: ToolBundle, identity_db: Database, audit_log
) -> None:
    """Callers get a generic ``tool_failed``; the detail lives in the audit trail."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(tools.registry, session, make_user(), "boom", {"value": 1})

    assert result.status == "error"
    assert result.error_kind == "execution"
    assert result.error == "tool_failed"
    assert "kapow" not in result.model_dump_json()
    [event] = await audit_log(identity_db, action="tool.failed")
    assert event.result == {"error": "kapow"}


async def test_invalid_output_returns_error(tools: ToolBundle, identity_db: Database) -> None:
    """A handler return violating ``output_model`` is an execution error."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            tools.registry, session, make_user(), "bad_output", {"value": 1}
        )

    assert result.status == "error"
    assert result.error_kind == "execution"
    assert result.error == "invalid tool output"


async def test_unknown_tool_returns_not_found_error(
    tools: ToolBundle, identity_db: Database
) -> None:
    """An unregistered name is reported as a not_found error result."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(tools.registry, session, make_user(), "ghost_tool", {})

    assert result.status == "error"
    assert result.error_kind == "not_found"
    assert "ghost_tool" in (result.error or "")


# --- Per-request approver role and precheck (T11.a) ------------------------------


class PrecheckSpy:
    """Async precheck double: records contexts and inputs, then passes or raises."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.contexts: list[ToolContext] = []
        self.inputs: list[DemoInput] = []

    async def __call__(self, context: ToolContext, validated_input: DemoInput) -> None:
        self.contexts.append(context)
        self.inputs.append(validated_input)
        if self.error is not None:
            raise self.error


def tier_by_value(validated_input: DemoInput) -> UserRole:
    """Owner approves values above 100; anything else asks for a mere viewer."""
    return UserRole.OWNER if validated_input.value > 100 else UserRole.VIEWER


def gated_registry(**overrides) -> ToolRegistry:
    """Registry with one operator-invoked ``gated_action`` ACTION (fields overridable)."""
    fields = {
        "name": "gated_action",
        "description": "operator action with optional approver resolver and precheck",
        "kind": ToolKind.ACTION,
        "approval_policy": ApprovalPolicy.REQUIRE_APPROVAL,
        "required_role": UserRole.OPERATOR,
        "handler": SpyHandler({"sent": True}),
        "input_model": DemoInput,
    }
    registry = ToolRegistry()
    registry.register(ToolSpec(**{**fields, **overrides}))
    return registry


@pytest.mark.parametrize(
    ("approval_role", "value", "expected"),
    [
        (None, 500, UserRole.OPERATOR),
        (tier_by_value, 500, UserRole.OWNER),
        (tier_by_value, 5, UserRole.OPERATOR),
    ],
    ids=["no-resolver", "resolver-more-privileged", "resolver-below-invocation-role"],
)
def test_resolved_approver_is_never_below_the_invocation_role(
    approval_role, value: int, expected: UserRole
) -> None:
    """The approver is the more privileged of the resolver's role and ``required_role``."""
    spec = gated_registry(approval_role=approval_role).get("gated_action")

    assert resolve_approval_role(spec, DemoInput(value=value)) is expected


def _broken_resolver(validated_input: DemoInput) -> UserRole:
    raise LookupError("tier table missing")


@pytest.mark.parametrize(
    "approval_role",
    [_broken_resolver, lambda validated_input: "superuser"],
    ids=["raises", "not-a-role"],
)
def test_failing_resolver_never_yields_a_guessed_role(approval_role) -> None:
    """A resolver that raises or returns a non-role is a typed tool error."""
    spec = gated_registry(approval_role=approval_role).get("gated_action")

    with pytest.raises(ToolApprovalRoleError):
        resolve_approval_role(spec, DemoInput(value=1))


async def test_approval_required_carries_the_resolved_approver_role(
    identity_db: Database, audit_log
) -> None:
    """The approval payload and its audit record name the per-request approver."""
    registry = gated_registry(approval_role=tier_by_value)
    operator = make_user(UserRole.OPERATOR)
    async with identity_db.sessionmaker() as session:
        high = await execute_tool(registry, session, operator, "gated_action", {"value": 500})
        low = await execute_tool(registry, session, operator, "gated_action", {"value": 5})

    assert high.approval_request.required_role == "owner"
    assert low.approval_request.required_role == "operator"
    events = await audit_log(identity_db, action="tool.approval_required")
    assert [event.result["required_role"] for event in events] == ["owner", "operator"]


async def test_failing_resolver_is_a_masked_tool_failure_without_a_request(
    identity_db: Database, audit_log
) -> None:
    """No approval payload is produced with a guessed role; the detail is audited."""
    handler = SpyHandler({"sent": True})
    registry = gated_registry(approval_role=_broken_resolver, handler=handler)
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(registry, session, make_user(), "gated_action", {"value": 1})

    assert (result.status, result.error_kind, result.error) == ("error", "execution", "tool_failed")
    assert result.approval_request is None
    assert "tier table" not in result.model_dump_json()
    assert handler.calls == []
    [failed] = await audit_log(identity_db, action="tool.failed")
    assert "tier table missing" in failed.result["error"]
    assert await audit_log(identity_db, action="tool.approval_required") == []


@pytest.mark.parametrize("dry_run", [False, True], ids=["approval-gate", "dry-run"])
async def test_precheck_refusal_stops_the_call_before_the_approval_gate(
    dry_run: bool, identity_db: Database, audit_log
) -> None:
    """A refusal returns ``precheck_failed`` with the safe reason and is audited."""
    precheck = PrecheckSpy(ToolPrecheckError("invoice already registered"))
    handler = SpyHandler({"sent": True})
    registry = gated_registry(handler=handler, precheck=precheck)
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            registry,
            session,
            make_user(UserRole.OPERATOR),
            "gated_action",
            {"value": 3},
            dry_run=dry_run,
        )

    assert (result.status, result.error_kind) == ("error", "precheck")
    assert result.error == "precheck_failed: invoice already registered"
    assert (result.approval_request, result.output) == (None, None)
    assert precheck.inputs == [DemoInput(value=3)]
    assert handler.calls == []
    [refused] = await audit_log(identity_db, action="tool.precheck_failed")
    assert (refused.actor_user_id, refused.tool_name) == (7, "gated_action")
    assert refused.payload == {"value": 3}
    assert refused.result == {"error": "precheck_failed", "reason": "invoice already registered"}
    assert await audit_log(identity_db, action="tool.approval_required") == []


async def test_precheck_runs_again_on_approved_calls_before_the_handler(
    identity_db: Database,
) -> None:
    """With a grant the precheck still decides; it sees the handler's own context."""
    precheck = PrecheckSpy(ToolPrecheckError("state changed since approval"))
    handler = SpyHandler({"sent": True})
    registry = gated_registry(handler=handler, precheck=precheck)
    key = compute_action_key("gated_action", DemoInput(value=3))
    async with identity_db.sessionmaker() as session:
        refused = await execute_tool(
            registry,
            session,
            make_user(),
            "gated_action",
            {"value": 3},
            approval_grant=mint_grant(uuid4(), key),
        )
        precheck.error = None
        executed = await execute_tool(
            registry,
            session,
            make_user(),
            "gated_action",
            {"value": 3},
            approval_grant=mint_grant(uuid4(), key),
        )

    assert (refused.status, refused.error_kind) == ("error", "precheck")
    assert executed.status == "ok"
    assert len(handler.calls) == 1
    assert precheck.contexts[-1] is handler.contexts[0]


async def test_unexpected_precheck_exception_is_masked_as_tool_failed(
    identity_db: Database, audit_log
) -> None:
    """Only ``ToolPrecheckError`` is a refusal; anything else hides its detail."""
    handler = SpyHandler({"sent": True})
    precheck = PrecheckSpy(RuntimeError("database at 10.0.0.5 unreachable"))
    registry = gated_registry(handler=handler, precheck=precheck)
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(registry, session, make_user(), "gated_action", {"value": 3})

    assert (result.status, result.error_kind, result.error) == ("error", "execution", "tool_failed")
    assert "10.0.0.5" not in result.model_dump_json()
    assert handler.calls == []
    [failed] = await audit_log(identity_db, action="tool.failed")
    assert failed.result == {"error": "database at 10.0.0.5 unreachable"}
    assert await audit_log(identity_db, action="tool.precheck_failed") == []


async def test_handler_refusal_is_reported_like_a_precheck_refusal(
    identity_db: Database, audit_log
) -> None:
    """A handler's ``ToolRefusalError`` reaches the caller as ``precheck_failed: <reason>``."""
    handler = SpyHandler({"sent": True}, error=ToolRefusalError("folder already exists"))
    registry = gated_registry(handler=handler)
    key = compute_action_key("gated_action", DemoInput(value=3))
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            registry,
            session,
            make_user(),
            "gated_action",
            {"value": 3},
            approval_grant=mint_grant(uuid4(), key),
        )

    assert (result.status, result.error_kind) == ("error", "precheck")
    assert result.error == "precheck_failed: folder already exists"
    assert len(handler.calls) == 1
    [refused] = await audit_log(identity_db, action="tool.precheck_failed")
    assert refused.result == {"error": "precheck_failed", "reason": "folder already exists"}
    assert await audit_log(identity_db, action="tool.failed") == []


async def test_plain_precheck_error_raised_by_a_handler_stays_masked(
    identity_db: Database, audit_log
) -> None:
    """Only ``ToolRefusalError`` escapes a handler; a bare ``ToolPrecheckError`` is masked."""
    handler = SpyHandler({"sent": True}, error=ToolPrecheckError("token expired"))
    registry = gated_registry(handler=handler)
    key = compute_action_key("gated_action", DemoInput(value=3))
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            registry,
            session,
            make_user(),
            "gated_action",
            {"value": 3},
            approval_grant=mint_grant(uuid4(), key),
        )

    assert (result.status, result.error_kind, result.error) == ("error", "execution", "tool_failed")
    [failed] = await audit_log(identity_db, action="tool.failed")
    assert failed.result == {"error": "token expired"}


# --- Module gating (T2) -------------------------------------------------------


async def test_inactive_module_tool_is_refused_before_validation_approval_and_handler(
    identity_db: Database, audit_log
) -> None:
    """An inactive module answers first — even invalid input never gets validated."""
    handler = SpyHandler({"sent": True})
    precheck = PrecheckSpy()
    registry = gated_registry(module="calendar", handler=handler, precheck=precheck)
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            registry,
            session,
            make_user(UserRole.OPERATOR),
            "gated_action",
            {"value": 0},  # invalid (ge=1): the module gate must answer before validation
            active_modules=set(),
        )

    assert (result.status, result.error_kind, result.error) == (
        "error",
        "inactive_module",
        "inactive_module",
    )
    assert (result.approval_request, result.output) == (None, None)
    assert handler.calls == []
    assert precheck.contexts == []
    [event] = await audit_log(identity_db, action="tool.inactive_module")
    assert (event.actor_user_id, event.tool_name) == (7, "gated_action")
    assert event.payload == {"input_fields": ["value"]}
    assert event.result == {"error": "inactive_module", "module": "calendar"}
    assert await audit_log(identity_db, action="tool.invalid_input") == []
    assert await audit_log(identity_db, action="tool.approval_required") == []


async def test_module_tool_passes_the_gate_when_the_module_is_active(
    identity_db: Database,
) -> None:
    """Membership in ``active_modules`` lets the call continue to the approval gate."""
    registry = gated_registry(module="calendar")
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            registry,
            session,
            make_user(UserRole.OPERATOR),
            "gated_action",
            {"value": 3},
            active_modules={"calendar"},
        )

    assert result.status == "approval_required"


async def test_core_tool_runs_whatever_the_active_module_set(
    tools: ToolBundle, identity_db: Database
) -> None:
    """``module=None`` is core: even an empty active set never gates it."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            tools.registry,
            session,
            make_user(),
            "spy_extract",
            {"value": 2},
            active_modules=set(),
        )

    assert result.status == "ok"


async def test_without_active_modules_there_is_no_module_gate(
    identity_db: Database, audit_log
) -> None:
    """``active_modules=None`` (the default) keeps the pre-T2 behavior: no gating."""
    registry = gated_registry(module="calendar")
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            registry, session, make_user(UserRole.OPERATOR), "gated_action", {"value": 3}
        )

    assert result.status == "approval_required"
    assert await audit_log(identity_db, action="tool.inactive_module") == []


# --- Builtin tools wired on the default registry -----------------------------

#: Acceptance criterion 5: every production tool is listed here on purpose, so
#: adding one (and checking it exposes no shell, SQL, or filesystem) is a
#: deliberate, reviewed change rather than a side effect of an import.
#: calendar-mvp allowlist change, human-approved 2026-10-15; google-mail
#: G1+G3 allowlist change, human-approved 2026-10-18 (six scoped Gmail
#: tools: three free reads, three approval-gated actions); agent-workspace L1
#: allowlist change: the two connector-backed reads of the user's granted
#: folder (their ``path`` is relative to that folder and jailed by the
#: connector; the server never opens a file).
BUILTIN_TOOL_ALLOWLIST = {
    "search_documents",
    "list_files",
    "read_file",
    "write_file",
    "create_folder",
    "move_path",
    "delete_path",
    "list_events",
    "create_event",
    "cancel_event",
    "search_emails",
    "get_email",
    "list_labels",
    "send_email",
    "archive_message",
    "add_label",
}

#: Input field names that would hint at shell, SQL, or filesystem access.
FORBIDDEN_INPUT_FIELDS = {"command", "cmd", "shell", "script", "sql", "path", "file_path"}

#: Connector tools take a *relative* folder path on purpose: it is validated
#: server-side and jailed again by the local connector.
CONNECTOR_PATH_TOOLS = {
    "list_files",
    "read_file",
    "write_file",
    "create_folder",
    "move_path",
    "delete_path",
}


async def _tenant_id(db: Database, slug: str) -> int:
    async with db.sessionmaker() as session:
        tenant = (await session.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one()
        return tenant.id


async def _seed_document(db: Database, tenant_id: int, filename: str, contents: list[str]) -> UUID:
    """Insert a processed document with one chunk per content (no vectors)."""
    async with db.sessionmaker() as session:
        document = Document(
            id=uuid4(),
            tenant_id=tenant_id,
            filename=filename,
            content_type="text/plain",
            size_bytes=1,
            storage_key=f"{tenant_id}/{filename}",
            status=DocumentStatus.PROCESSED,
        )
        session.add(document)
        await session.flush()
        cursor = 0
        for index, content in enumerate(contents):
            session.add(
                DocumentChunk(
                    document_id=document.id,
                    tenant_id=tenant_id,
                    chunk_index=index,
                    content=content,
                    start_offset=cursor,
                    end_offset=cursor + len(content),
                )
            )
            cursor += len(content)
        await session.commit()
        return document.id


def test_builtin_search_documents_is_a_viewer_auto_extract() -> None:
    """Importing ``builtin`` registers a read-only, auto-approved search tool."""
    spec = default_registry.get("search_documents")

    assert spec is builtin.SEARCH_DOCUMENTS
    assert spec.kind is ToolKind.EXTRACT
    assert spec.approval_policy is ApprovalPolicy.AUTO
    assert spec.required_role is UserRole.VIEWER
    assert spec.output_model is builtin.SearchDocumentsOutput


def test_default_registry_exposes_only_allowlisted_tools() -> None:
    """No production tool may appear without being added to the allowlist."""
    assert set(default_registry.names()) == BUILTIN_TOOL_ALLOWLIST


def test_builtin_inputs_are_closed_and_expose_no_shell_sql_or_path() -> None:
    """Builtin inputs forbid extra fields and carry no shell/SQL arguments.

    The connector tools are the reviewed exception: their only argument is a
    relative path inside the folder the user granted, and the connector jails
    it again.
    """
    for spec in default_registry.list_specs():
        assert spec.input_model is not None
        assert spec.input_model.model_config.get("extra") == "forbid", spec.name
        forbidden = (
            FORBIDDEN_INPUT_FIELDS - {"path"}
            if spec.name in CONNECTOR_PATH_TOOLS
            else FORBIDDEN_INPUT_FIELDS
        )
        assert not set(spec.input_model.model_fields) & forbidden, spec.name


async def test_search_documents_returns_only_callers_tenant_hits(seeded_db: Database) -> None:
    """Each tenant only sees its own chunks, each with a verifiable citation."""
    acme = await _tenant_id(seeded_db, "acme")
    globex = await _tenant_id(seeded_db, "globex")
    acme_doc = await _seed_document(
        seeded_db, acme, "acme.txt", ["unrelated notes ", "acme invoice total 100"]
    )
    globex_doc = await _seed_document(seeded_db, globex, "globex.txt", ["globex invoice total"])

    async with seeded_db.sessionmaker() as session:
        acme_result = await execute_tool(
            default_registry,
            session,
            make_user(UserRole.VIEWER, tenant_id=acme),
            "search_documents",
            {"query": "invoice"},
        )
        globex_result = await execute_tool(
            default_registry,
            session,
            make_user(UserRole.VIEWER, tenant_id=globex),
            "search_documents",
            {"query": "invoice"},
        )

    assert acme_result.status == "ok"
    acme_hits = acme_result.output.hits
    assert [hit.document_id for hit in acme_hits] == [acme_doc]
    hit = acme_hits[0]
    assert hit.document_filename == "acme.txt"
    assert hit.content == "acme invoice total 100"
    assert (hit.start_offset, hit.end_offset) == (16, 16 + len(hit.content))
    assert globex_result.status == "ok"
    assert [hit.document_id for hit in globex_result.output.hits] == [globex_doc]


async def test_search_documents_honors_top_k(seeded_db: Database) -> None:
    """``top_k`` bounds how many hits the tool returns."""
    acme = await _tenant_id(seeded_db, "acme")
    await _seed_document(seeded_db, acme, "many.txt", ["invoice one", "invoice two", "invoice 3"])

    async with seeded_db.sessionmaker() as session:
        result = await execute_tool(
            default_registry,
            session,
            make_user(UserRole.VIEWER, tenant_id=acme),
            "search_documents",
            {"query": "invoice", "top_k": 2},
        )

    assert result.status == "ok"
    assert len(result.output.hits) == 2


async def test_search_documents_rejects_tenant_override_in_input(seeded_db: Database) -> None:
    """A ``tenant_id`` smuggled into the input is a validation error, not a scope."""
    acme = await _tenant_id(seeded_db, "acme")
    globex = await _tenant_id(seeded_db, "globex")
    await _seed_document(seeded_db, globex, "globex.txt", ["globex invoice total"])

    async with seeded_db.sessionmaker() as session:
        result = await execute_tool(
            default_registry,
            session,
            make_user(UserRole.VIEWER, tenant_id=acme),
            "search_documents",
            {"query": "invoice", "tenant_id": globex},
        )

    assert result.status == "error"
    assert result.error_kind == "validation"
    assert "tenant_id" in (result.error or "")
    assert result.output is None


@pytest.mark.parametrize(
    "raw_input",
    [
        {},
        {"query": ""},
        {"query": "x" * 501},
        {"query": "invoice", "top_k": 0},
        {"query": "invoice", "top_k": 21},
        {"query": "invoice", "mode": "regex"},
    ],
    ids=[
        "missing-query",
        "empty-query",
        "query-too-long",
        "top-k-zero",
        "top-k-too-big",
        "unknown-mode",
    ],
)
async def test_search_documents_rejects_unbounded_input(
    raw_input: dict, identity_db: Database
) -> None:
    """Query length and ``top_k`` are bounded before any search runs."""
    async with identity_db.sessionmaker() as session:
        result = await execute_tool(
            default_registry, session, make_user(UserRole.VIEWER), "search_documents", raw_input
        )

    assert result.status == "error"
    assert result.error_kind == "validation"


async def test_search_documents_dry_run_skips_the_search(seeded_db: Database) -> None:
    """A dry run describes the call and returns no hits."""
    acme = await _tenant_id(seeded_db, "acme")
    await _seed_document(seeded_db, acme, "acme.txt", ["acme invoice total"])

    async with seeded_db.sessionmaker() as session:
        result = await execute_tool(
            default_registry,
            session,
            make_user(UserRole.VIEWER, tenant_id=acme),
            "search_documents",
            {"query": "invoice"},
            dry_run=True,
        )

    assert result.status == "dry_run"
    assert result.output.validated_input == {"query": "invoice", "top_k": 5, "mode": "hybrid"}
    assert result.output.approval_required is False
    assert not hasattr(result.output, "hits")


class RecordingEmbeddings(FakeEmbeddingProvider):
    """Fake embedder that records queries and can be told to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(dimension=64)
        self.fail = fail
        self.queries: list[str] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.queries.extend(texts)
        if self.fail:
            raise EmbeddingError("embedding backend down")
        return await super().embed_texts(texts)


async def _seed_embedded(db: Database, tenant_id: int, contents: list[str]) -> None:
    document_id = await _seed_document(db, tenant_id, "embedded.txt", contents)
    vectors = await FakeEmbeddingProvider(dimension=64).embed_texts(contents)
    async with db.sessionmaker() as session:
        chunks = (
            await session.execute(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
                .order_by(DocumentChunk.chunk_index)
            )
        ).scalars()
        for chunk, vector in zip(chunks, vectors, strict=True):
            chunk.embedding = vector
        await session.commit()


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
async def test_search_documents_embeds_the_query_with_the_context_provider(
    seeded_db: Database, mode: str
) -> None:
    """Semantic and hybrid modes rank with the provider from the ``ToolContext``."""
    acme = await _tenant_id(seeded_db, "acme")
    await _seed_embedded(seeded_db, acme, ["invoice total 100", "holiday calendar"])
    embeddings = RecordingEmbeddings()

    async with seeded_db.sessionmaker() as session:
        result = await execute_tool(
            default_registry,
            session,
            make_user(UserRole.VIEWER, tenant_id=acme),
            "search_documents",
            {"query": "invoice total", "mode": mode},
            embeddings=embeddings,
        )

    assert result.status == "ok"
    assert result.output.mode == mode
    assert embeddings.queries == ["invoice total"]
    assert result.output.hits[0].content == "invoice total 100"


@pytest.mark.parametrize("embeddings", [None, RecordingEmbeddings(fail=True)], ids=["none", "down"])
async def test_search_documents_degrades_to_keyword_without_working_embeddings(
    seeded_db: Database, embeddings: RecordingEmbeddings | None
) -> None:
    """No provider, or a failing one, still answers with keyword ranking."""
    acme = await _tenant_id(seeded_db, "acme")
    await _seed_embedded(seeded_db, acme, ["invoice total 100", "holiday calendar"])

    async with seeded_db.sessionmaker() as session:
        result = await execute_tool(
            default_registry,
            session,
            make_user(UserRole.VIEWER, tenant_id=acme),
            "search_documents",
            {"query": "invoice"},
            embeddings=embeddings,
        )

    assert result.status == "ok"
    assert result.output.mode == "keyword"
    assert [hit.content for hit in result.output.hits] == ["invoice total 100"]
