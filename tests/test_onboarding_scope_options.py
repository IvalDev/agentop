"""Onboarding engine: dynamic ``scope_select`` options hook (google-mail M2).

Contract under test (the G2 engine extension, risk #1 of the google-mail
plan): the engine stays module-agnostic while a module may register a
*scope options provider* — the same explicit-registry style as
:func:`register_tester <app.modules.onboarding.testers.register_tester>`:

- with no provider registered, ``scope_select`` keeps its static contract
  (options = the manifest's declared ``permissions`` — covered by the
  pre-existing ``tests/test_onboarding_api.py`` cases);
- a registered provider is invoked when the engine *renders* the step
  (GET state) and when it *validates* a submit; its options replace the
  static allowlist, and an ``exclusive`` provider accepts exactly one
  choice;
- a provider failure (:class:`ScopeOptionsError`, or any unexpected raise)
  fails closed on BOTH paths: the rendered step still answers 200 with an
  empty option list and one fixed actionable ``help_text`` (never a 500,
  never provider internals), and a submit is refused 409 with that same
  fixed message (the SPA's 409 → reload loop then shows the step's error
  state).
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.crypto import generate_credentials_key
from app.core.db import Database
from app.main import create_app
from app.modules.catalog.manifest import ModuleManifest, StepSpec
from app.modules.catalog.registry import ModuleCatalog
from app.modules.onboarding.options import (
    ScopeOptions,
    ScopeOptionsContext,
    ScopeOptionsError,
    register_scope_options_provider,
    scope_options_providers,
)

BASE = "/api/v1/onboarding"
DYNAMIC = "demo_dynamic"
JWT_SECRET = "test-secret-0123456789-abcdefghijklmnop"
PERMISSIONS = ("perm.a", "perm.b")
#: The engine's one fixed actionable message for an options failure.
UNAVAILABLE = "scope options could not be loaded; reconnect the account and try again"


@pytest.fixture(autouse=True)
def options_registry_clean() -> AsyncIterator[None]:
    """The process-wide scope-options registry starts and ends empty per test."""
    scope_options_providers.clear()
    yield
    scope_options_providers.clear()


@pytest.fixture
def dynamic_catalog() -> ModuleCatalog:
    """Local catalog: a scope_select-first module with static permissions."""
    catalog = ModuleCatalog()
    catalog.register(
        ModuleManifest(
            key=DYNAMIC,
            name="Demo Dynamic Scopes",
            description="test-only module exercising the scope options hook",
            version="1.0.0",
            permissions=PERMISSIONS,
            onboarding=(
                StepSpec(key="pick", type="scope_select", title="Pick a scope"),
                StepSpec(key="verify", type="test_connection", title="Verify"),
            ),
        )
    )
    return catalog


@pytest.fixture
def dynamic_settings() -> Settings:
    return Settings(
        jwt_secret=JWT_SECRET,
        access_token_expire_minutes=30,
        credentials_key=generate_credentials_key(),
    )


@pytest.fixture
def testers() -> dict[str, Any]:
    return {}


@pytest.fixture
def dynamic_app(
    seeded_db: Database, dynamic_settings: Settings, dynamic_catalog: ModuleCatalog, testers: dict
) -> FastAPI:
    application = create_app()
    application.state.db = seeded_db
    application.state.settings = dynamic_settings
    application.state.module_catalog = dynamic_catalog
    application.state.connection_testers = testers
    return application


@pytest.fixture
async def dynamic_client(dynamic_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=dynamic_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _headers(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "owner@acme.test", "password": "owner-pass", "tenant_slug": "acme"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _activate(client: AsyncClient, headers: dict[str, str]) -> None:
    response = await client.post(f"/api/v1/modules/{DYNAMIC}/activate", headers=headers)
    assert response.status_code == 200, response.text


def _register(
    options: ScopeOptions | Exception,
) -> None:
    """Register a provider returning ``options`` or raising it."""

    async def provider(context: ScopeOptionsContext) -> ScopeOptions:
        if isinstance(options, Exception):
            raise options
        return options

    register_scope_options_provider(DYNAMIC, provider)


# --- rendering (GET state) ---------------------------------------------------


async def test_scope_select_renders_registered_dynamic_options(
    dynamic_client: AsyncClient,
) -> None:
    """The step's options come from the provider, not from manifest.permissions."""
    headers = await _headers(dynamic_client)
    await _activate(dynamic_client, headers)
    _register(ScopeOptions(options=("choice.x", "choice.y")))

    response = await dynamic_client.get(f"{BASE}/{DYNAMIC}", headers=headers)

    step = response.json()["step"]
    assert step["type"] == "scope_select"
    assert step["fields"] == [
        {
            "key": "scopes",
            "label": "Scopes",
            "type": "select",
            "required": True,
            "secret": False,
            "help_text": "",
            "options": ["choice.x", "choice.y"],
            "option_labels": {},
        }
    ]


async def test_scope_select_renders_provider_labels_for_offered_values_only(
    dynamic_client: AsyncClient,
) -> None:
    """Display labels ride beside the values; stale or blank labels are dropped."""
    headers = await _headers(dynamic_client)
    await _activate(dynamic_client, headers)
    _register(
        ScopeOptions(
            options=("choice.x", "choice.y", "choice.z"),
            labels={"choice.x": "Choice X", "choice.y": "", "stale.w": "Not offered"},
        )
    )

    response = await dynamic_client.get(f"{BASE}/{DYNAMIC}", headers=headers)

    field = response.json()["step"]["fields"][0]
    assert field["options"] == ["choice.x", "choice.y", "choice.z"]
    assert field["option_labels"] == {"choice.x": "Choice X"}


async def test_scope_options_failure_renders_an_actionable_error_state(
    dynamic_client: AsyncClient,
) -> None:
    """GET still answers 200: empty options plus the fixed help_text."""
    headers = await _headers(dynamic_client)
    await _activate(dynamic_client, headers)
    _register(ScopeOptionsError("token details that must never leak"))

    response = await dynamic_client.get(f"{BASE}/{DYNAMIC}", headers=headers)

    assert response.status_code == 200
    field = response.json()["step"]["fields"][0]
    assert field["options"] == []
    assert field["option_labels"] == {}
    assert field["help_text"] == UNAVAILABLE
    assert "token" not in response.text


async def test_unexpected_provider_crash_also_fails_closed_on_read(
    dynamic_client: AsyncClient,
) -> None:
    """Any raise becomes the same error state, never a 500."""
    headers = await _headers(dynamic_client)
    await _activate(dynamic_client, headers)

    async def boom(context: ScopeOptionsContext) -> ScopeOptions:
        raise RuntimeError("upstream exploded with internals")

    register_scope_options_provider(DYNAMIC, boom)

    response = await dynamic_client.get(f"{BASE}/{DYNAMIC}", headers=headers)

    assert response.status_code == 200
    field = response.json()["step"]["fields"][0]
    assert field["options"] == []
    assert field["help_text"] == UNAVAILABLE
    assert "internals" not in response.text


# --- validation (submit) -----------------------------------------------------


async def test_submit_accepts_a_dynamic_choice_outside_the_permissions(
    dynamic_client: AsyncClient, testers: dict
) -> None:
    """Dynamic options REPLACE the static allowlist on submit."""
    headers = await _headers(dynamic_client)
    await _activate(dynamic_client, headers)
    _register(ScopeOptions(options=("choice.x", "choice.y")))

    response = await dynamic_client.post(
        f"{BASE}/{DYNAMIC}/steps/pick",
        json={"answers": {"scopes": ["choice.x"]}},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_step"] == "verify"
    assert body["data"] == {"scopes": ["choice.x"]}


async def test_submit_rejects_a_static_permission_not_in_the_dynamic_options(
    dynamic_client: AsyncClient,
) -> None:
    """A declared permission is no longer choosable once the provider speaks."""
    headers = await _headers(dynamic_client)
    await _activate(dynamic_client, headers)
    _register(ScopeOptions(options=("choice.x",)))

    response = await dynamic_client.post(
        f"{BASE}/{DYNAMIC}/steps/pick",
        json={"answers": {"scopes": ["perm.a"]}},
        headers=headers,
    )

    assert response.status_code == 422


async def test_submit_validates_values_never_display_labels(
    dynamic_client: AsyncClient,
) -> None:
    """Labels are display only: the allowlist stays the option values."""
    headers = await _headers(dynamic_client)
    await _activate(dynamic_client, headers)
    _register(ScopeOptions(options=("choice.x",), labels={"choice.x": "Choice X"}))

    by_label = await dynamic_client.post(
        f"{BASE}/{DYNAMIC}/steps/pick",
        json={"answers": {"scopes": ["Choice X"]}},
        headers=headers,
    )
    by_value = await dynamic_client.post(
        f"{BASE}/{DYNAMIC}/steps/pick",
        json={"answers": {"scopes": ["choice.x"]}},
        headers=headers,
    )

    assert by_label.status_code == 422
    assert by_value.status_code == 200, by_value.text
    assert by_value.json()["data"] == {"scopes": ["choice.x"]}


async def test_exclusive_provider_enforces_exactly_one_choice(
    dynamic_client: AsyncClient,
) -> None:
    """MVP single-label rule: two checked boxes are a 422; one is accepted."""
    headers = await _headers(dynamic_client)
    await _activate(dynamic_client, headers)
    _register(ScopeOptions(options=("choice.x", "choice.y"), exclusive=True))

    both = await dynamic_client.post(
        f"{BASE}/{DYNAMIC}/steps/pick",
        json={"answers": {"scopes": ["choice.x", "choice.y"]}},
        headers=headers,
    )
    one = await dynamic_client.post(
        f"{BASE}/{DYNAMIC}/steps/pick",
        json={"answers": {"scopes": ["choice.y"]}},
        headers=headers,
    )

    assert both.status_code == 422
    assert one.status_code == 200, one.text
    assert one.json()["data"] == {"scopes": ["choice.y"]}


async def test_submit_when_options_are_unavailable_is_409(
    dynamic_client: AsyncClient,
) -> None:
    """Fail closed: a stale submit cannot choose against unverifiable options."""
    headers = await _headers(dynamic_client)
    await _activate(dynamic_client, headers)
    _register(ScopeOptionsError("no labels for you"))

    response = await dynamic_client.post(
        f"{BASE}/{DYNAMIC}/steps/pick",
        json={"answers": {"scopes": ["choice.x"]}},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == UNAVAILABLE


async def test_without_a_registered_provider_the_static_contract_stays(
    dynamic_client: AsyncClient,
) -> None:
    """No provider: manifest.permissions remain the allowlist (regression guard).

    The rejected dynamic choice goes FIRST: a refusal must leave the state
    untouched (still on ``pick``), so the static permission is then accepted
    on the same step.
    """
    headers = await _headers(dynamic_client)
    await _activate(dynamic_client, headers)

    get_response = await dynamic_client.get(f"{BASE}/{DYNAMIC}", headers=headers)
    rejected = await dynamic_client.post(
        f"{BASE}/{DYNAMIC}/steps/pick",
        json={"answers": {"scopes": ["choice.x"]}},
        headers=headers,
    )
    accepted = await dynamic_client.post(
        f"{BASE}/{DYNAMIC}/steps/pick",
        json={"answers": {"scopes": ["perm.a"]}},
        headers=headers,
    )

    assert get_response.json()["step"]["fields"][0]["options"] == list(PERMISSIONS)
    assert get_response.json()["step"]["fields"][0]["option_labels"] == {}
    assert rejected.status_code == 422
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["current_step"] == "verify"


async def test_the_options_provider_receives_the_caller_and_settings(
    dynamic_client: AsyncClient, dynamic_settings: Settings
) -> None:
    """Context contract: principal, session, module, credentials key, settings."""
    headers = await _headers(dynamic_client)
    await _activate(dynamic_client, headers)
    seen: dict[str, Any] = {}

    async def provider(context: ScopeOptionsContext) -> ScopeOptions:
        seen["tenant_id"] = context.caller.tenant_id
        seen["user_id"] = context.caller.id
        seen["module_key"] = context.module_key
        seen["credentials_key"] = context.credentials_key
        seen["settings"] = context.settings
        return ScopeOptions(options=("choice.x",))

    register_scope_options_provider(DYNAMIC, provider)

    await dynamic_client.get(f"{BASE}/{DYNAMIC}", headers=headers)

    assert seen["tenant_id"] == 1
    assert seen["user_id"] == 1
    assert seen["module_key"] == DYNAMIC
    assert seen["credentials_key"] == dynamic_settings.credentials_key.get_secret_value()
    assert seen["settings"] is dynamic_settings
