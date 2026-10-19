"""Calendar chat tools (T3): specs, validation, precheck, refresh, handlers.

Contract under test (D5/D6 of calendar-mvp):

- three ToolSpecs on a LOCAL registry via ``register_calendar_tools`` —
  never ``default_registry`` (the T1 human decision stays in force: the
  production import graph registers nothing);
- ``list_events`` is EXTRACT / viewer; ``create_event`` and ``cancel_event``
  are ACTION / operator with ``ALLOW_WITHOUT_APPROVAL`` (D5: the user's own
  calendar is self-service, so the model never asks for approval);
- input validation: bounded ``max_results``, ordered ``from``/``to``, a
  non-empty summary, ``end > start``, timezone-aware datetimes;
- the shared precheck never leaks secrets and never 500s: no connection
  row, no credentials blob, a missing or unusable ``credentials_key``, and
  an unreadable blob all answer one actionable ``precheck_failed`` reason;
  an expired access token is refreshed on demand (D3) and persisted back on
  the connection, while a failed refresh answers the "reconnect" reason;
- happy paths decrypt the stored token, build the per-user provider, and
  return output the ``output_model`` accepts;
- the executor's module gate refuses an inactive calendar module before the
  precheck ever runs.
"""

import json
import time
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import Settings
from app.core.crypto import decrypt_credentials, encrypt_credentials, generate_credentials_key
from app.core.db import Database
from app.modules.calendar.provider import (
    CalendarProviderError,
    provider_registry,
    register_provider,
)
from app.modules.calendar.tools import (
    EXPIRED,
    NOT_CONFIGURED,
    register_calendar_tools,
)
from app.modules.connections.models import ConnectionStatus, UserConnection
from app.modules.identity.models import UserRole
from app.modules.oauth import service as oauth_service
from app.modules.tools.executor import execute_tool
from app.modules.tools.registry import (
    ApprovalPolicy,
    ToolKind,
    ToolRegistry,
)

JWT_SECRET = "test-secret-0123456789-abcdefghijklmnop"
VIEWER = "viewer@acme.test"
ADMIN = "admin@acme.test"
OWNER = "owner@acme.test"

LIST_INPUT = {"from": "2026-10-20T08:00:00+02:00", "to": "2026-10-20T18:00:00+02:00"}
CREATE_INPUT = {
    "summary": "Dentist",
    "start": "2026-10-21T09:00:00+02:00",
    "end": "2026-10-21T09:30:00+02:00",
}
CANCEL_INPUT = {"event_id": "evt-1"}


@pytest.fixture(autouse=True)
def _clean_provider_registry() -> Any:
    """The process-wide provider registry starts and ends empty per test."""
    provider_registry.clear()
    yield
    provider_registry.clear()


@pytest.fixture
def oauth_key() -> str:
    return generate_credentials_key()


@pytest.fixture
def calendar_registry() -> ToolRegistry:
    """A LOCAL registry holding the three calendar tools (never the default)."""
    registry = ToolRegistry()
    register_calendar_tools(registry)
    return registry


@pytest.fixture
def calendar_settings(oauth_key: str) -> Settings:
    """Settings carrying the encryption key plus Google client credentials."""
    return Settings(
        jwt_secret=JWT_SECRET,
        credentials_key=oauth_key,
        google_client_id=SecretStr("gid"),
        google_client_secret=SecretStr("gsecret"),
    )


class FakeCalendar:
    """In-memory CalendarProvider factory double.

    Records every constructed provider's access token and every call. With
    ``explode=True`` the factory itself raises — tests use it to prove the
    precheck never builds a provider.
    """

    def __init__(
        self,
        *,
        items: list[dict[str, Any]] | None = None,
        created: dict[str, Any] | None = None,
        create_error: Exception | None = None,
        explode: bool = False,
    ) -> None:
        self.explode = explode
        self.items = items if items is not None else []
        self.created = created if created is not None else {"id": "evt-new"}
        self.create_error = create_error
        self.access_tokens: list[str] = []
        self.list_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def __call__(self, *, access_token: str) -> "FakeCalendarProvider":
        if self.explode:
            raise AssertionError("provider factory must not be called")
        self.access_tokens.append(access_token)
        return FakeCalendarProvider(self)


class FakeCalendarProvider:
    """Per-call provider handed out by :class:`FakeCalendar`."""

    def __init__(self, factory: FakeCalendar) -> None:
        self._factory = factory

    async def list_events(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._factory.list_calls.append(kwargs)
        return list(self._factory.items)

    async def create_event(self, **kwargs: Any) -> dict[str, Any]:
        self._factory.create_calls.append(kwargs)
        if self._factory.create_error is not None:
            raise self._factory.create_error
        return dict(self._factory.created)

    async def delete_event(self, **kwargs: Any) -> None:
        self._factory.delete_calls.append(kwargs)


async def seed_connection(
    db: Database,
    user: Any,
    *,
    key: str,
    access_token: str = "ya29.stored",
    refresh_token: str | None = "rt-1",
    expires_at: int | None = None,
    scopes: list[str] | None = None,
    with_blob: bool = True,
) -> None:
    """Insert the user's connected calendar row with an encrypted blob."""
    blob: dict[str, Any] = {
        "access_token": access_token,
        "scopes": scopes
        if scopes is not None
        else ["https://www.googleapis.com/auth/calendar.events"],
    }
    if refresh_token is not None:
        blob["refresh_token"] = refresh_token
    if expires_at is not None:
        blob["expires_at"] = expires_at
    encrypted = encrypt_credentials(key, json.dumps(blob)) if with_blob else None
    async with db.sessionmaker() as session:
        session.add(
            UserConnection(
                tenant_id=user.tenant_id,
                user_id=user.id,
                module_key="calendar",
                provider="google",
                status=ConnectionStatus.CONNECTED,
                credentials_encrypted=encrypted,
            )
        )
        await session.commit()


async def stored_blob(db: Database, user: Any, key: str) -> dict[str, Any]:
    """Decrypt the user's stored calendar credentials (test assertions only)."""
    async with db.sessionmaker() as session:
        row = (
            await session.execute(
                select(UserConnection).where(
                    UserConnection.tenant_id == user.tenant_id,
                    UserConnection.user_id == user.id,
                    UserConnection.module_key == "calendar",
                )
            )
        ).scalar_one()
        assert row.credentials_encrypted is not None
        return json.loads(decrypt_credentials(key, row.credentials_encrypted))


async def run_tool(
    db: Database,
    user: Any,
    registry: ToolRegistry,
    tool: str,
    raw_input: dict[str, Any],
    *,
    credentials_key: str | None = None,
    settings: Settings | None = None,
    active_modules: set[str] | None = None,
):
    """One ``execute_tool`` call on a fresh session (what the agent loop does)."""
    async with db.sessionmaker() as session:
        return await execute_tool(
            registry,
            session,
            user,
            tool,
            raw_input,
            credentials_key=credentials_key,
            settings=settings,
            active_modules=active_modules if active_modules is not None else {"calendar"},
        )


# --- Spec shapes and input validation ------------------------------------------


def test_register_calendar_tools_registers_three_specs_on_a_local_registry() -> None:
    """ACTION tools may declare ALLOW_WITHOUT_APPROVAL (D5 self-service)."""
    registry = ToolRegistry()

    register_calendar_tools(registry)

    assert registry.names() == ["list_events", "create_event", "cancel_event"]


def test_tool_specs_declare_module_roles_kinds_and_policies(
    calendar_registry: ToolRegistry,
) -> None:
    listed = calendar_registry.get("list_events")
    assert (listed.kind, listed.approval_policy, listed.required_role) == (
        ToolKind.EXTRACT,
        ApprovalPolicy.ALLOW_WITHOUT_APPROVAL,
        UserRole.VIEWER,
    )
    for name in ("create_event", "cancel_event"):
        spec = calendar_registry.get(name)
        assert (spec.kind, spec.approval_policy, spec.required_role) == (
            ToolKind.ACTION,
            ApprovalPolicy.ALLOW_WITHOUT_APPROVAL,
            UserRole.OPERATOR,
        )
        assert spec.module == "calendar"
    assert listed.module == "calendar"
    assert all(spec.precheck is not None for spec in calendar_registry.list_specs())


@pytest.mark.parametrize(
    "raw_input",
    [
        {"to": "2026-10-20T18:00:00+02:00"},  # from missing
        {"from": "2026-10-20T08:00:00+02:00"},  # to missing
        {"from": "not-a-datetime", "to": "2026-10-20T18:00:00+02:00"},
        {"from": "2026-10-20T08:00:00", "to": "2026-10-20T18:00:00"},  # naive
        {"from": "2026-10-20T18:00:00+02:00", "to": "2026-10-20T08:00:00+02:00"},  # reversed
        {**LIST_INPUT, "max_results": 0},
        {**LIST_INPUT, "max_results": 51},
        {**LIST_INPUT, "extra": "smuggled"},
    ],
)
async def test_list_events_rejects_invalid_input(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, raw_input: dict
) -> None:
    result = await run_tool(
        seeded_db, seeded_users[OWNER], calendar_registry, "list_events", raw_input
    )

    assert (result.status, result.error_kind) == ("error", "validation")
    assert result.error is not None and result.error.startswith("invalid input")


@pytest.mark.parametrize(
    "raw_input",
    [
        {"summary": "", "start": CREATE_INPUT["start"], "end": CREATE_INPUT["end"]},
        {"summary": "X", "start": "2026-10-21T10:00:00+02:00", "end": CREATE_INPUT["start"]},
        {"summary": "X", "start": CREATE_INPUT["start"], "end": CREATE_INPUT["start"]},
        {"summary": "X", "start": "2026-10-21T09:00:00", "end": "2026-10-21T09:30:00"},  # naive
        {"summary": "X", "start": CREATE_INPUT["start"], "end": "bad"},
        {**CREATE_INPUT, "attendees": ["ok@example.com", ""]},
        {**CREATE_INPUT, "tenant_id": 999},
    ],
)
async def test_create_event_rejects_invalid_input(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, raw_input: dict
) -> None:
    result = await run_tool(
        seeded_db, seeded_users[OWNER], calendar_registry, "create_event", raw_input
    )

    assert (result.status, result.error_kind) == ("error", "validation")


@pytest.mark.parametrize("raw_input", [{}, {"event_id": ""}, {"event_id": "x", "extra": 1}])
async def test_cancel_event_rejects_invalid_input(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, raw_input: dict
) -> None:
    result = await run_tool(
        seeded_db, seeded_users[OWNER], calendar_registry, "cancel_event", raw_input
    )

    assert (result.status, result.error_kind) == ("error", "validation")


# --- Precheck matrix -----------------------------------------------------------

ALL_TOOLS_AND_INPUTS = [
    ("list_events", LIST_INPUT),
    ("create_event", CREATE_INPUT),
    ("cancel_event", CANCEL_INPUT),
]


@pytest.mark.parametrize(("tool", "raw_input"), ALL_TOOLS_AND_INPUTS)
async def test_no_connection_row_prechecks_with_actionable_reason(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, tool, raw_input
) -> None:
    fake = FakeCalendar(explode=True)
    register_provider("google", fake)

    result = await run_tool(
        seeded_db, seeded_users[OWNER], calendar_registry, tool, raw_input, credentials_key="k"
    )

    assert (result.status, result.error_kind) == ("error", "precheck")
    assert result.error == f"precheck_failed: {NOT_CONFIGURED}"


@pytest.mark.parametrize(("tool", "raw_input"), ALL_TOOLS_AND_INPUTS)
async def test_row_without_credentials_blob_prechecks(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key, tool, raw_input
) -> None:
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key, with_blob=False)
    fake = FakeCalendar(explode=True)
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        calendar_registry,
        tool,
        raw_input,
        credentials_key=oauth_key,
    )

    assert result.error == f"precheck_failed: {NOT_CONFIGURED}"


async def test_missing_credentials_key_prechecks(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key
) -> None:
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    register_provider("google", FakeCalendar(explode=True))

    result = await run_tool(
        seeded_db, seeded_users[OWNER], calendar_registry, "list_events", LIST_INPUT
    )

    assert result.error == f"precheck_failed: {NOT_CONFIGURED}"


async def test_unreadable_blob_prechecks_without_leaking(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key
) -> None:
    """A blob encrypted under another key is 'not configured', never a 500."""
    await seed_connection(seeded_db, seeded_users[OWNER], key=generate_credentials_key())
    register_provider("google", FakeCalendar(explode=True))

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        calendar_registry,
        "list_events",
        LIST_INPUT,
        credentials_key=oauth_key,
    )

    assert (result.status, result.error_kind) == ("error", "precheck")
    assert result.error == f"precheck_failed: {NOT_CONFIGURED}"
    assert oauth_key not in (result.error or "")


async def test_expired_token_refreshes_persists_and_runs(
    seeded_db: Database,
    seeded_users,
    calendar_registry: ToolRegistry,
    oauth_key,
    calendar_settings,
    monkeypatch,
) -> None:
    """An expired access token is refreshed (D3) and the new blob is stored."""
    await seed_connection(
        seeded_db,
        seeded_users[OWNER],
        key=oauth_key,
        access_token="ya29.old",
        refresh_token="rt-keep",
        expires_at=int(time.time()) - 120,
    )
    seen: dict[str, Any] = {}

    async def fake_refresh(**kwargs: Any) -> oauth_service.OAuthTokens:
        seen["kwargs"] = kwargs
        return oauth_service.OAuthTokens(
            access_token="ya29.new", refresh_token=None, expires_in=3600, scopes=()
        )

    monkeypatch.setattr(oauth_service, "refresh_access_token", fake_refresh)
    fake = FakeCalendar(items=[{"id": "e1", "summary": "Sync"}])
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        calendar_registry,
        "list_events",
        LIST_INPUT,
        credentials_key=oauth_key,
        settings=calendar_settings,
    )

    assert result.status == "ok"
    assert seen["kwargs"] == {
        "refresh_token": "rt-keep",
        "client_id": "gid",
        "client_secret": "gsecret",
    }
    assert fake.access_tokens == ["ya29.new"]
    blob = await stored_blob(seeded_db, seeded_users[OWNER], oauth_key)
    assert blob["access_token"] == "ya29.new"
    # Google may omit the refresh token on refresh: the stored one survives.
    assert blob["refresh_token"] == "rt-keep"
    assert blob["expires_at"] > time.time()


async def test_expired_token_refresh_failure_prechecks_reconnect(
    seeded_db: Database,
    seeded_users,
    calendar_registry: ToolRegistry,
    oauth_key,
    calendar_settings,
    monkeypatch,
) -> None:
    async def refused_refresh(**kwargs: Any) -> oauth_service.OAuthTokens:
        raise oauth_service.TokenExchangeError("token endpoint answered 400")

    monkeypatch.setattr(oauth_service, "refresh_access_token", refused_refresh)
    register_provider("google", FakeCalendar(explode=True))
    await seed_connection(
        seeded_db, seeded_users[OWNER], key=oauth_key, expires_at=int(time.time()) - 120
    )

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        calendar_registry,
        "list_events",
        LIST_INPUT,
        credentials_key=oauth_key,
        settings=calendar_settings,
    )

    assert (result.status, result.error_kind) == ("error", "precheck")
    assert result.error == f"precheck_failed: {EXPIRED}"


async def test_expired_token_without_refresh_token_prechecks_reconnect(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key, calendar_settings
) -> None:
    register_provider("google", FakeCalendar(explode=True))
    await seed_connection(
        seeded_db,
        seeded_users[OWNER],
        key=oauth_key,
        refresh_token=None,
        expires_at=int(time.time()) - 120,
    )

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        calendar_registry,
        "list_events",
        LIST_INPUT,
        credentials_key=oauth_key,
        settings=calendar_settings,
    )

    assert result.error == f"precheck_failed: {EXPIRED}"


async def test_expired_token_without_google_client_settings_prechecks_reconnect(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key
) -> None:
    """No configured Google client credentials means refresh is impossible."""
    register_provider("google", FakeCalendar(explode=True))
    await seed_connection(
        seeded_db, seeded_users[OWNER], key=oauth_key, expires_at=int(time.time()) - 120
    )
    settings = Settings(jwt_secret=JWT_SECRET, credentials_key=oauth_key)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        calendar_registry,
        "list_events",
        LIST_INPUT,
        credentials_key=oauth_key,
        settings=settings,
    )

    assert result.error == f"precheck_failed: {EXPIRED}"


async def test_live_token_is_not_refreshed(
    seeded_db: Database,
    seeded_users,
    calendar_registry: ToolRegistry,
    oauth_key,
    calendar_settings,
    monkeypatch,
) -> None:
    async def exploding_refresh(**kwargs: Any) -> oauth_service.OAuthTokens:
        raise AssertionError("a live token must not be refreshed")

    monkeypatch.setattr(oauth_service, "refresh_access_token", exploding_refresh)
    fake = FakeCalendar(items=[])
    register_provider("google", fake)
    await seed_connection(
        seeded_db, seeded_users[OWNER], key=oauth_key, expires_at=int(time.time()) + 3600
    )

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        calendar_registry,
        "list_events",
        LIST_INPUT,
        credentials_key=oauth_key,
        settings=calendar_settings,
    )

    assert result.status == "ok"
    assert fake.access_tokens == ["ya29.stored"]


# --- Happy paths ---------------------------------------------------------------


async def test_list_events_returns_validated_events_and_passes_the_range(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key
) -> None:
    items = [
        {
            "id": "e1",
            "summary": "Standup",
            "start": {"dateTime": "2026-10-20T09:00:00+02:00"},
            "end": {"dateTime": "2026-10-20T09:15:00+02:00"},
            "attendees": [{"email": "a@example.com"}, {"email": "b@example.com"}],
            "status": "confirmed",
        },
        {"id": "e2", "start": {"date": "2026-10-20"}, "end": {"date": "2026-10-20"}},
    ]
    fake = FakeCalendar(items=items)
    register_provider("google", fake)
    await seed_connection(seeded_db, seeded_users[VIEWER], key=oauth_key)

    result = await run_tool(
        seeded_db,
        seeded_users[VIEWER],
        calendar_registry,
        "list_events",
        {**LIST_INPUT, "max_results": 5, "calendar_id": "team@example.com"},
        credentials_key=oauth_key,
    )

    assert result.status == "ok"
    events = result.output.events
    assert [event.id for event in events] == ["e1", "e2"]
    assert events[0].summary == "Standup"
    assert events[0].attendees == ["a@example.com", "b@example.com"]
    assert events[0].status == "confirmed"
    assert events[1].start == "2026-10-20"  # all-day date survives the mapping
    assert fake.list_calls == [
        {
            "calendar_id": "team@example.com",
            "max_results": 5,
            "time_min": "2026-10-20T08:00:00+02:00",
            "time_max": "2026-10-20T18:00:00+02:00",
        }
    ]
    assert fake.access_tokens == ["ya29.stored"]


async def test_create_event_returns_id_and_html_link(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key
) -> None:
    fake = FakeCalendar(
        created={"id": "evt-new", "html_link": "https://cal/evt-new", "status": "confirmed"}
    )
    register_provider("google", fake)
    await seed_connection(seeded_db, seeded_users[ADMIN], key=oauth_key)

    result = await run_tool(
        seeded_db,
        seeded_users[ADMIN],
        calendar_registry,
        "create_event",
        {**CREATE_INPUT, "description": "Checkup", "attendees": ["ana@example.com"]},
        credentials_key=oauth_key,
    )

    assert result.status == "ok"
    assert (result.output.id, result.output.html_link) == ("evt-new", "https://cal/evt-new")
    assert fake.create_calls == [
        {
            "summary": "Dentist",
            "start": "2026-10-21T09:00:00+02:00",
            "end": "2026-10-21T09:30:00+02:00",
            "description": "Checkup",
            "attendees": ["ana@example.com"],
        }
    ]
    assert fake.access_tokens == ["ya29.stored"]


async def test_cancel_event_returns_confirmation(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key
) -> None:
    fake = FakeCalendar()
    register_provider("google", fake)
    await seed_connection(seeded_db, seeded_users[ADMIN], key=oauth_key)

    result = await run_tool(
        seeded_db,
        seeded_users[ADMIN],
        calendar_registry,
        "cancel_event",
        {**CANCEL_INPUT, "calendar_id": "team@example.com"},
        credentials_key=oauth_key,
    )

    assert result.status == "ok"
    assert (result.output.cancelled, result.output.event_id) == (True, "evt-1")
    assert fake.delete_calls == [{"calendar_id": "team@example.com", "event_id": "evt-1"}]


async def test_provider_failure_is_a_masked_tool_failure(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key
) -> None:
    fake = FakeCalendar(create_error=CalendarProviderError("calendar API answered 500"))
    register_provider("google", fake)
    await seed_connection(seeded_db, seeded_users[ADMIN], key=oauth_key)

    result = await run_tool(
        seeded_db,
        seeded_users[ADMIN],
        calendar_registry,
        "create_event",
        CREATE_INPUT,
        credentials_key=oauth_key,
    )

    assert (result.status, result.error, result.error_kind) == ("error", "tool_failed", "execution")


# --- Executor integration -------------------------------------------------------


async def test_inactive_module_is_refused_before_the_precheck(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key
) -> None:
    fake = FakeCalendar(explode=True)
    register_provider("google", fake)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        calendar_registry,
        "list_events",
        LIST_INPUT,
        credentials_key=oauth_key,
        active_modules=set(),
    )

    assert (result.status, result.error, result.error_kind) == (
        "error",
        "inactive_module",
        "inactive_module",
    )


async def test_viewer_may_not_create_or_cancel(
    seeded_db: Database, seeded_users, calendar_registry: ToolRegistry, oauth_key
) -> None:
    register_provider("google", FakeCalendar(explode=True))
    await seed_connection(seeded_db, seeded_users[VIEWER], key=oauth_key)

    for tool, raw_input in (("create_event", CREATE_INPUT), ("cancel_event", CANCEL_INPUT)):
        result = await run_tool(
            seeded_db,
            seeded_users[VIEWER],
            calendar_registry,
            tool,
            raw_input,
            credentials_key=oauth_key,
        )
        assert (result.status, result.error, result.error_kind) == (
            "error",
            "forbidden",
            "forbidden",
        )
