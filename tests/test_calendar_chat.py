"""Chat wiring of the calendar tools (D6, T3): schemas and credential plumbing.

Contract under test:

- ``tool_schemas`` offers the three calendar tools when the tenant's active
  module set contains ``calendar`` and hides them otherwise (the T2 gating),
  and still filters by the user's role (viewer sees only ``list_events``);
- ``AgentRun``/``AgentDeps`` carry ``credentials_key`` into every
  ``execute_tool`` call: a full agent run over a local registry with a real
  (fake-provider) connection decrypts the stored token and succeeds, while
  the same run without the key hits the precheck and the model receives the
  actionable reason.

Calendar tools are ``ALLOW_WITHOUT_APPROVAL`` (D5), so they never flow
through the approvals service — that path keeps its default
``credentials_key=None`` and stays untouched.
"""

import json
import time

import pytest

from app.core.config import Settings
from app.core.crypto import encrypt_credentials, generate_credentials_key
from app.core.db import Database
from app.modules.agent.llm import AssistantMessage, FakeLLMProvider, ToolCall
from app.modules.agent.service import AgentDeps, run_agent, tool_schemas
from app.modules.calendar.provider import provider_registry, register_provider
from app.modules.calendar.tools import EXPIRED, NOT_CONFIGURED, register_calendar_tools
from app.modules.catalog.models import ModuleActivation
from app.modules.connections.models import ConnectionStatus, UserConnection
from app.modules.identity.deps import CurrentUser
from app.modules.retrieval.embeddings import FakeEmbeddingProvider
from app.modules.tools.registry import ToolRegistry

JWT_SECRET = "test-secret-0123456789-abcdefghijklmnop"
DIM = 64
OWNER = "owner@acme.test"
VIEWER = "viewer@acme.test"
CALENDAR_TOOLS = {"list_events", "create_event", "cancel_event"}
LIST_INPUT = {"from": "2026-10-20T08:00:00+02:00", "to": "2026-10-20T18:00:00+02:00"}


@pytest.fixture(autouse=True)
def _clean_provider_registry():
    provider_registry.clear()
    yield
    provider_registry.clear()


@pytest.fixture
def calendar_registry() -> ToolRegistry:
    """A LOCAL registry with the three calendar tools (never the default)."""
    registry = ToolRegistry()
    register_calendar_tools(registry)
    return registry


@pytest.fixture
def oauth_key() -> str:
    return generate_credentials_key()


def call(name: str, arguments: dict | None = None) -> AssistantMessage:
    tool_call = ToolCall(name=name, arguments={} if arguments is None else arguments)
    return AssistantMessage(content="", tool_calls=(tool_call,))


def answer(text: str) -> AssistantMessage:
    return AssistantMessage(content=text)


async def activate_calendar(db: Database, tenant_id: int, user_id: int) -> None:
    async with db.sessionmaker() as session:
        session.add(
            ModuleActivation(
                tenant_id=tenant_id,
                module_key="calendar",
                active=True,
                activated_by_user_id=user_id,
            )
        )
        await session.commit()


async def seed_connection(
    db: Database, user: CurrentUser, *, key: str, expires_in: int = 3600
) -> None:
    blob = json.dumps(
        {
            "access_token": "ya29.chat-wired",
            "refresh_token": "rt-1",
            "expires_at": int(time.time()) + expires_in,
            "scopes": ["https://www.googleapis.com/auth/calendar.events"],
        }
    )
    async with db.sessionmaker() as session:
        session.add(
            UserConnection(
                tenant_id=user.tenant_id,
                user_id=user.id,
                module_key="calendar",
                provider="google",
                status=ConnectionStatus.CONNECTED,
                credentials_encrypted=encrypt_credentials(key, blob),
            )
        )
        await session.commit()


class ListFactory:
    """Provider factory double returning one canned event and recording tokens."""

    def __init__(self) -> None:
        self.access_tokens: list[str] = []

    def __call__(self, *, access_token: str):
        self.access_tokens.append(access_token)
        return self

    async def list_events(self, **kwargs):
        return [
            {
                "id": "e1",
                "summary": "ChatSync",
                "start": {"dateTime": "2026-10-20T09:00:00+02:00"},
                "end": {"dateTime": "2026-10-20T09:30:00+02:00"},
            }
        ]

    async def create_event(self, **kwargs):
        return {"id": "e2", "html_link": "https://cal/e2"}

    async def delete_event(self, **kwargs):
        return None


async def run(db: Database, user: CurrentUser, agent_deps: AgentDeps, message: str = "Help"):
    async with db.sessionmaker() as session:
        return await run_agent(session, user, message, [], agent_deps)


def deps(registry: ToolRegistry, llm, *, credentials_key: str | None = None) -> AgentDeps:
    return AgentDeps(
        registry=registry,
        llm=llm,
        embeddings=FakeEmbeddingProvider(dimension=DIM),
        settings=Settings(jwt_secret=JWT_SECRET, approval_ttl_seconds=3600),
        credentials_key=credentials_key,
    )


# --- D6: schema filtering ------------------------------------------------------


def test_tool_schemas_include_the_calendar_tools_when_the_module_is_active(
    seeded_users, calendar_registry: ToolRegistry
) -> None:
    schemas = tool_schemas(calendar_registry, seeded_users[OWNER], {"calendar"})

    names = {schema["function"]["name"] for schema in schemas}
    assert names == CALENDAR_TOOLS
    list_schema = next(s for s in schemas if s["function"]["name"] == "list_events")
    properties = list_schema["function"]["parameters"]["properties"]
    assert set(properties) == {"from", "to", "max_results", "calendar_id"}


def test_tool_schemas_exclude_the_calendar_tools_when_the_module_is_inactive(
    seeded_users, calendar_registry: ToolRegistry
) -> None:
    assert tool_schemas(calendar_registry, seeded_users[OWNER], set()) == []
    assert tool_schemas(calendar_registry, seeded_users[OWNER], {"mail"}) == []


def test_tool_schemas_still_filter_by_role(seeded_users, calendar_registry: ToolRegistry) -> None:
    viewer = seeded_users[VIEWER]

    names = {s["function"]["name"] for s in tool_schemas(calendar_registry, viewer, {"calendar"})}

    assert names == {"list_events"}


# --- AgentRun credential plumbing ----------------------------------------------


async def test_agent_run_decrypts_the_stored_token_and_lists_events(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key
) -> None:
    """A full chat run carries credentials_key into the tool and succeeds."""
    owner = seeded_users[OWNER]
    await activate_calendar(seeded_db, owner.tenant_id, owner.id)
    await seed_connection(seeded_db, owner, key=oauth_key)
    factory = ListFactory()
    register_provider("google", factory)
    llm = FakeLLMProvider([call("list_events", LIST_INPUT), answer("You have one event.")])

    response = await run(seeded_db, owner, deps(calendar_registry, llm, credentials_key=oauth_key))

    assert response.answer == "You have one event."
    assert [(i.tool_name, i.status) for i in response.tool_calls] == [("list_events", "ok")]
    assert factory.access_tokens == ["ya29.chat-wired"]


async def test_agent_run_without_credentials_key_reaches_the_model_as_precheck(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key
) -> None:
    """No key in AgentDeps ⇒ the precheck refuses with the actionable reason."""
    owner = seeded_users[OWNER]
    await activate_calendar(seeded_db, owner.tenant_id, owner.id)
    await seed_connection(seeded_db, owner, key=oauth_key)
    llm = FakeLLMProvider([call("list_events", LIST_INPUT), answer("Please connect.")])

    response = await run(seeded_db, owner, deps(calendar_registry, llm))

    assert response.tool_calls[0].error == "precheck_failed"
    # The model sees the detailed reason (executor message reaches the loop).
    tool_msg = llm.calls[1].messages[-1]
    assert tool_msg.role == "tool"
    assert NOT_CONFIGURED in tool_msg.content


async def test_agent_run_surfaces_the_expired_reason_when_refresh_is_impossible(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key
) -> None:
    """An expired token without Google client settings tells the model to reconnect."""
    owner = seeded_users[OWNER]
    await activate_calendar(seeded_db, owner.tenant_id, owner.id)
    await seed_connection(seeded_db, owner, key=oauth_key, expires_in=-120)
    llm = FakeLLMProvider([call("list_events", LIST_INPUT), answer("Please reconnect.")])

    response = await run(seeded_db, owner, deps(calendar_registry, llm, credentials_key=oauth_key))

    assert response.tool_calls[0].error == "precheck_failed"
    tool_msg = llm.calls[1].messages[-1]
    assert EXPIRED in tool_msg.content


async def test_inactive_calendar_module_never_reaches_the_precheck_in_chat(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key
) -> None:
    """Without a ModuleActivation row the loop refuses as inactive_module."""
    owner = seeded_users[OWNER]
    llm = FakeLLMProvider([call("list_events", LIST_INPUT), answer("Not available.")])

    response = await run(seeded_db, owner, deps(calendar_registry, llm, credentials_key=oauth_key))

    assert response.tool_calls[0].error == "inactive_module"
