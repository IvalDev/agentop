"""Tool registry tests: registration invariants and lookup errors.

Covers the T6 registration contract: valid specs register and round-trip;
duplicate names, invalid names, missing input models, the
ACTION-must-require-approval invariant, the T11 hook invariants
(``approval_role`` only on ``REQUIRE_APPROVAL`` tools, callable hooks), the
google-mail G7 invariant (``requester_only_approval`` only on
``REQUIRE_APPROVAL`` tools), and the T2 module invariant (``module``
defaults to ``None`` for core tools; a declared key must be a lowercase
identifier) all raise
``ToolRegistrationError``; unknown lookups raise ``ToolNotFoundError``.
"""

import pytest
from pydantic import BaseModel

from app.modules.identity.models import UserRole
from app.modules.tools.exceptions import ToolNotFoundError, ToolRegistrationError
from app.modules.tools.registry import ApprovalPolicy, ToolKind, ToolRegistry, ToolSpec

INVALID_NAMES = ["Send Email", "send-email", "2fast", "", "UPPER_snake", " spaced"]


class DemoInput(BaseModel):
    """Typed input used by the registry tests."""

    value: int


async def stub_handler(context, validated_input) -> dict:
    """Minimal async handler satisfying the ToolSpec handler contract."""
    return {"value": validated_input.value}


def build_spec(
    name: str = "demo_tool",
    *,
    kind: ToolKind = ToolKind.EXTRACT,
    approval_policy: ApprovalPolicy = ApprovalPolicy.AUTO,
    required_role: UserRole = UserRole.OPERATOR,
    input_model: type[BaseModel] | None = DemoInput,
    handler=stub_handler,
    approval_role=None,
    precheck=None,
    requester_only_approval: bool = False,
) -> ToolSpec:
    """Build a ToolSpec with test defaults; individual fields can be overridden."""
    return ToolSpec(
        name=name,
        description="registry test tool",
        kind=kind,
        approval_policy=approval_policy,
        required_role=required_role,
        handler=handler,
        input_model=input_model,
        approval_role=approval_role,
        precheck=precheck,
        requester_only_approval=requester_only_approval,
    )


def build_moduled_spec(module) -> ToolSpec:
    """A spec declaring ``module`` — only the module-invariant tests use it."""
    return ToolSpec(
        name="demo_tool",
        description="registry test tool",
        kind=ToolKind.EXTRACT,
        approval_policy=ApprovalPolicy.AUTO,
        required_role=UserRole.OPERATOR,
        handler=stub_handler,
        input_model=DemoInput,
        module=module,
    )


def owner_approves(validated_input) -> UserRole:
    """Approver-role resolver used by the hook invariant tests."""
    return UserRole.OWNER


async def stub_precheck(context, validated_input) -> None:
    """Precheck that always lets the call through."""


def test_register_and_get_round_trip() -> None:
    """A valid spec registers and is retrievable by name."""
    registry = ToolRegistry()
    spec = build_spec()

    registry.register(spec)

    assert registry.get("demo_tool") is spec


def test_names_and_list_specs_reflect_registrations() -> None:
    """``names`` and ``list_specs`` return every registered tool."""
    registry = ToolRegistry()
    first = build_spec("alpha_tool")
    second = build_spec("beta_tool")

    registry.register(first)
    registry.register(second)

    assert registry.names() == ["alpha_tool", "beta_tool"]
    assert registry.list_specs() == [first, second]


def test_duplicate_name_raises() -> None:
    """Registering the same name twice is a registration error."""
    registry = ToolRegistry()
    registry.register(build_spec("demo_tool"))

    with pytest.raises(ToolRegistrationError, match="duplicate"):
        registry.register(build_spec("demo_tool"))


def test_action_with_auto_policy_raises() -> None:
    """An ACTION tool may never run without human approval."""
    registry = ToolRegistry()
    spec = build_spec(kind=ToolKind.ACTION, approval_policy=ApprovalPolicy.AUTO)

    with pytest.raises(ToolRegistrationError, match="approval"):
        registry.register(spec)


def test_action_with_require_approval_registers() -> None:
    """ACTION + REQUIRE_APPROVAL is the legal combination."""
    registry = ToolRegistry()
    spec = build_spec(kind=ToolKind.ACTION, approval_policy=ApprovalPolicy.REQUIRE_APPROVAL)

    registry.register(spec)

    assert registry.get(spec.name).approval_policy is ApprovalPolicy.REQUIRE_APPROVAL


def test_extract_with_require_approval_registers() -> None:
    """EXTRACT tools may opt into approval as well as AUTO."""
    registry = ToolRegistry()
    spec = build_spec(kind=ToolKind.EXTRACT, approval_policy=ApprovalPolicy.REQUIRE_APPROVAL)

    registry.register(spec)

    assert registry.get(spec.name).kind is ToolKind.EXTRACT


@pytest.mark.parametrize("bad_name", INVALID_NAMES)
def test_invalid_name_raises(bad_name: str) -> None:
    """Names must be lowercase identifier-ish strings (``extract_invoice``)."""
    registry = ToolRegistry()

    with pytest.raises(ToolRegistrationError, match="name"):
        registry.register(build_spec(name=bad_name))


def test_module_defaults_to_none_for_core_tools() -> None:
    """A spec that declares no module is a core tool: always available."""
    registry = ToolRegistry()

    registry.register(build_spec())

    assert registry.get("demo_tool").module is None


def test_valid_module_key_registers_and_round_trips() -> None:
    """A module tool keeps its catalog key through registration."""
    registry = ToolRegistry()

    registry.register(build_moduled_spec("calendar"))

    assert registry.get("demo_tool").module == "calendar"


@pytest.mark.parametrize("bad_module", [*INVALID_NAMES, 42])
def test_invalid_module_raises(bad_module) -> None:
    """Module keys follow the same lowercase-identifier pattern as tool names."""
    registry = ToolRegistry()

    with pytest.raises(ToolRegistrationError, match="module"):
        registry.register(build_moduled_spec(bad_module))


def test_missing_input_model_raises() -> None:
    """A spec without an input model cannot validate its arguments."""
    registry = ToolRegistry()
    spec = build_spec(input_model=None)

    with pytest.raises(ToolRegistrationError, match="input_model"):
        registry.register(spec)


def test_non_callable_handler_raises() -> None:
    """A spec whose handler is not callable is rejected at registration."""
    registry = ToolRegistry()
    spec = build_spec(handler="not-a-callable")

    with pytest.raises(ToolRegistrationError, match="handler"):
        registry.register(spec)


def test_approval_role_on_an_auto_tool_raises() -> None:
    """A per-request approver only makes sense on a tool that needs approval."""
    registry = ToolRegistry()
    spec = build_spec(approval_policy=ApprovalPolicy.AUTO, approval_role=owner_approves)

    with pytest.raises(ToolRegistrationError, match="approval_role"):
        registry.register(spec)


def test_approval_role_and_precheck_register_on_an_approval_tool() -> None:
    """Both hooks are accepted on a REQUIRE_APPROVAL tool and kept on the spec."""
    registry = ToolRegistry()
    spec = build_spec(
        kind=ToolKind.ACTION,
        approval_policy=ApprovalPolicy.REQUIRE_APPROVAL,
        approval_role=owner_approves,
        precheck=stub_precheck,
    )

    registry.register(spec)

    stored = registry.get(spec.name)
    assert (stored.approval_role, stored.precheck) == (owner_approves, stub_precheck)


def test_precheck_registers_on_an_auto_tool() -> None:
    """A precheck needs no approval policy: AUTO tools may verify state too."""
    registry = ToolRegistry()

    registry.register(build_spec(precheck=stub_precheck))

    assert registry.get("demo_tool").precheck is stub_precheck


def test_requester_only_approval_defaults_to_false() -> None:
    """Any satisfying role may decide unless a tool opts in explicitly."""
    registry = ToolRegistry()

    registry.register(build_spec())

    assert registry.get("demo_tool").requester_only_approval is False


def test_requester_only_approval_registers_on_an_approval_tool() -> None:
    """A tool acting on the requester's own connection keeps the flag."""
    registry = ToolRegistry()
    spec = build_spec(
        kind=ToolKind.ACTION,
        approval_policy=ApprovalPolicy.REQUIRE_APPROVAL,
        requester_only_approval=True,
    )

    registry.register(spec)

    assert registry.get(spec.name).requester_only_approval is True


@pytest.mark.parametrize(
    "policy", [ApprovalPolicy.AUTO, ApprovalPolicy.ALLOW_WITHOUT_APPROVAL], ids=str
)
def test_requester_only_approval_without_approval_raises(policy: ApprovalPolicy) -> None:
    """The flag restricts who decides an approval: meaningless on a tool that needs none."""
    registry = ToolRegistry()
    spec = build_spec(approval_policy=policy, requester_only_approval=True)

    with pytest.raises(ToolRegistrationError, match="requester_only_approval"):
        registry.register(spec)


@pytest.mark.parametrize("hook", ["approval_role", "precheck"])
def test_non_callable_hook_raises(hook: str) -> None:
    """Hooks are checked at registration, not at the first call."""
    registry = ToolRegistry()
    spec = build_spec(
        kind=ToolKind.ACTION,
        approval_policy=ApprovalPolicy.REQUIRE_APPROVAL,
        **{hook: "not-a-callable"},
    )

    with pytest.raises(ToolRegistrationError, match=hook):
        registry.register(spec)


def test_get_unknown_tool_raises_not_found() -> None:
    """Looking up an unregistered name raises ``ToolNotFoundError``."""
    registry = ToolRegistry()

    with pytest.raises(ToolNotFoundError, match="ghost_tool"):
        registry.get("ghost_tool")
