"""Tool registry tests: registration invariants and lookup errors.

Covers the T6 registration contract: valid specs register and round-trip;
duplicate names, invalid names, missing input models, and the
ACTION-must-require-approval invariant all raise ``ToolRegistrationError``;
unknown lookups raise ``ToolNotFoundError``.
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
    )


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


def test_get_unknown_tool_raises_not_found() -> None:
    """Looking up an unregistered name raises ``ToolNotFoundError``."""
    registry = ToolRegistry()

    with pytest.raises(ToolNotFoundError, match="ghost_tool"):
        registry.get("ghost_tool")
