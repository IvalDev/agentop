"""Google Mail chat tools (G3): specs, validation, precheck, scope, approval.

Contract under test (google-mail M3 / acceptance criteria 3–5):

- six ToolSpecs on a LOCAL registry via ``register_google_mail_tools`` —
  never ``default_registry`` directly (the production fold is asserted by
  the executor allowlist and the manifest tests);
- ``search_emails``, ``get_email``, and ``list_labels`` are EXTRACT /
  viewer with ``ALLOW_WITHOUT_APPROVAL`` (reading needs no confirmation);
  ``send_email``, ``archive_message``, and ``add_label`` are ACTION /
  operator with ``REQUIRE_APPROVAL``: without an approval grant the
  executor stops at ``approval_required`` and the Gmail provider is never
  called; with a minted grant the handler runs (criterion 4);
- every tool's precheck requires the persisted mailbox scope
  (``get_effective_scope``) — no connection, no selection, or an
  unreadable blob is one actionable ``NOT_CONFIGURED`` reason; an expired
  access token is refreshed on demand and persisted (criterion 5);
- app-side scope enforcement (criterion 3, risk #2): ``search_emails``
  always filters by ``labelIds=[selection]`` unless the scope is ``ALL``;
  ``get_email``, ``archive_message``, and ``add_label`` deny a message
  whose ``labelIds`` do not contain the selected label with the actionable
  ``OUT_OF_SCOPE`` reason (a precheck refusal: the executor surfaces it as
  ``precheck_failed`` before any handler or approval path runs);
  ``send_email`` is exempt from the label check (a new message belongs to
  no folder) but ALWAYS requires approval;
- enriched search (G3.1): ``search_emails`` reads every hit's metadata
  (``format=metadata``) concurrently within ``METADATA_CONCURRENCY`` and
  answers subject, sender, date, and snippet in Gmail's order; a hit
  deleted between list and get (404) is skipped, a hit whose metadata
  lacks the selected label is dropped (defense in depth), and any other
  failure is a masked ``tool_failed``;
- requester-only decisions (G7): the three actions act on the requester's
  own Gmail connection, so only the user who requested one may approve,
  reject, or execute it — another user of the tenant would otherwise send
  from (or modify) their own mailbox;
- well-formed sends (G8): ``send_email`` sets ``From`` to the connected
  address read from ``users.getProfile`` plus ``Date`` and ``Message-ID``;
  an unreadable profile or an unusable address sends nothing (401/403 →
  ``EXPIRED``, anything else → masked ``tool_failed``), and a send whose
  answer lacks the ``SENT`` label is never reported as sent;
- readable bodies (G8): ``get_email`` falls back to the first ``text/html``
  part converted to plain text when no ``text/plain`` part exists;
- there is no delete/trash tool, ever.
"""

import asyncio
import base64
import json
import re
import time
from datetime import UTC, datetime
from email import message_from_bytes
from email.policy import default
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import Settings
from app.core.crypto import decrypt_credentials, encrypt_credentials, generate_credentials_key
from app.core.db import Database
from app.modules.approvals.exceptions import ApprovalForbiddenError
from app.modules.approvals.grants import mint_grant
from app.modules.approvals.service import (
    approve_request,
    create_approval_request,
    execute_approved_action,
    reject_request,
)
from app.modules.catalog.models import ModuleActivation
from app.modules.connections.models import ConnectionStatus, UserConnection
from app.modules.google_mail import tools as mail_tools
from app.modules.google_mail.provider import (
    GOOGLE_GMAIL_MESSAGES_URL,
    GOOGLE_GMAIL_PROFILE_URL,
    GmailAuthenticationError,
    GmailNotFoundError,
    GmailProviderError,
    GoogleGmailProvider,
    provider_registry,
    register_provider,
)
from app.modules.google_mail.scope import ALL_SCOPE
from app.modules.google_mail.tools import (
    EXPIRED,
    METADATA_CONCURRENCY,
    NOT_CONFIGURED,
    OUT_OF_SCOPE,
    SEARCH_EMAILS,
    AddLabelInput,
    ArchiveMessageInput,
    SendEmailInput,
    register_google_mail_tools,
)
from app.modules.identity.deps import CurrentUser
from app.modules.identity.models import UserRole
from app.modules.oauth import service as oauth_service
from app.modules.onboarding.models import OnboardingState, OnboardingStatus
from app.modules.tools.exceptions import ToolPrecheckError
from app.modules.tools.executor import compute_action_key, execute_tool
from app.modules.tools.registry import (
    ApprovalPolicy,
    ToolKind,
    ToolRegistry,
)

JWT_SECRET = "test-secret-0123456789-abcdefghijklmnop"
OWNER = "owner@acme.test"
ADMIN = "admin@acme.test"
VIEWER = "viewer@acme.test"

SEARCH_INPUT: dict[str, Any] = {"query": "invoice", "max_results": 5}
GET_INPUT: dict[str, Any] = {"message_id": "m1"}
SEND_INPUT: dict[str, Any] = {
    "to": ["ana@example.com"],
    "subject": "Hola",
    "body": "¿Cómo estás?",
}
ARCHIVE_INPUT: dict[str, Any] = {"message_id": "m1"}
ADD_LABEL_INPUT: dict[str, Any] = {"message_id": "m1", "label_id": "Label_9"}

READ_TOOLS_AND_INPUTS = [
    ("search_emails", SEARCH_INPUT),
    ("get_email", GET_INPUT),
    ("list_labels", {}),
]
ACTION_TOOLS_AND_INPUTS = [
    ("send_email", SEND_INPUT),
    ("archive_message", ARCHIVE_INPUT),
    ("add_label", ADD_LABEL_INPUT),
]
ALL_TOOLS_AND_INPUTS = READ_TOOLS_AND_INPUTS + ACTION_TOOLS_AND_INPUTS

#: A full Gmail message resource (``format=full``) inside INBOX + Label_7.
IN_SCOPE_MESSAGE: dict[str, Any] = {
    "id": "m1",
    "threadId": "t-1",
    "labelIds": ["INBOX", "Label_7"],
    "snippet": "Please pay invoice 42.",
    "payload": {
        "mimeType": "text/plain",
        "headers": [
            {"name": "From", "value": "ana@example.com"},
            {"name": "To", "value": "me@acme.test"},
            {"name": "Subject", "value": "Invoice 42"},
            {"name": "Date", "value": "Fri, 16 Oct 2026 10:00:00 +0200"},
        ],
        "body": {"data": base64.urlsafe_b64encode(b"Please pay invoice 42.").decode("ascii")},
    },
}

#: Same shape, but only under a label outside the ``Label_7`` scope.
OUT_OF_SCOPE_MESSAGE: dict[str, Any] = {
    **IN_SCOPE_MESSAGE,
    "labelIds": ["INBOX"],
}


@pytest.fixture(autouse=True)
def _clean_provider_registry() -> Any:
    """The process-wide Gmail provider registry starts and ends empty per test."""
    provider_registry.clear()
    yield
    provider_registry.clear()


@pytest.fixture
def oauth_key() -> str:
    return generate_credentials_key()


@pytest.fixture
def mail_registry() -> ToolRegistry:
    """A LOCAL registry holding the six google_mail tools (never the default)."""
    registry = ToolRegistry()
    register_google_mail_tools(registry)
    return registry


@pytest.fixture
def mail_settings(oauth_key: str) -> Settings:
    """Settings carrying the encryption key plus Google client credentials."""
    return Settings(
        jwt_secret=JWT_SECRET,
        credentials_key=oauth_key,
        google_client_id=SecretStr("gid"),
        google_client_secret=SecretStr("gsecret"),
    )


class FakeGmail:
    """In-memory GmailProvider factory double.

    Records every constructed provider's access token and every call. With
    ``explode=True`` the factory itself raises — tests use it to prove the
    precheck never builds a provider.
    """

    def __init__(
        self,
        *,
        listed: list[dict[str, Any]] | None = None,
        message: dict[str, Any] | None = None,
        sent: dict[str, Any] | None = None,
        modified: dict[str, Any] | None = None,
        labels: list[dict[str, Any]] | None = None,
        metadata: dict[str, dict[str, Any]] | None = None,
        list_error: Exception | None = None,
        message_error: Exception | None = None,
        metadata_errors: dict[str, Exception] | None = None,
        metadata_delays: dict[str, int] | None = None,
        send_error: Exception | None = None,
        modify_error: Exception | None = None,
        profile: dict[str, Any] | None = None,
        profile_error: Exception | None = None,
        explode: bool = False,
    ) -> None:
        self.explode = explode
        self.profile = profile if profile is not None else {"emailAddress": "agent@acme.test"}
        self.profile_error = profile_error
        self.listed = listed if listed is not None else []
        # Per-id ``format=metadata`` resources; an id without an entry answers
        # its listed entry (enough for tests that only check the list call).
        self.metadata = metadata if metadata is not None else {}
        self.metadata_errors = metadata_errors if metadata_errors is not None else {}
        # Per-id event-loop yields before answering (out-of-order completion).
        self.metadata_delays = metadata_delays if metadata_delays is not None else {}
        self.message = message if message is not None else IN_SCOPE_MESSAGE
        # Gmail labels a delivered message SENT in the users.messagesSend answer.
        self.sent = (
            sent if sent is not None else {"id": "sent-1", "threadId": "t-9", "labelIds": ["SENT"]}
        )
        self.modified = modified if modified is not None else {"id": "m1"}
        self.labels = (
            labels
            if labels is not None
            else [
                {"id": "INBOX", "name": "Inbox", "type": "system"},
                {"id": "Label_7", "name": "Asistente", "type": "user"},
            ]
        )
        self.list_error = list_error
        self.message_error = message_error
        self.send_error = send_error
        self.modify_error = modify_error
        self.access_tokens: list[str] = []
        self.profile_calls = 0
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.metadata_calls: list[str] = []
        self.metadata_cancelled: list[str] = []
        self.metadata_in_flight = 0
        self.metadata_max_in_flight = 0
        self.send_calls: list[dict[str, Any]] = []
        self.modify_calls: list[dict[str, Any]] = []

    def __call__(self, *, access_token: str) -> "FakeGmailProvider":
        if self.explode:
            raise AssertionError("provider factory must not be called")
        self.access_tokens.append(access_token)
        return FakeGmailProvider(self)


class FakeGmailProvider:
    """Per-call provider handed out by :class:`FakeGmail`."""

    def __init__(self, factory: FakeGmail) -> None:
        self._factory = factory

    async def get_profile(self) -> dict[str, Any]:
        self._factory.profile_calls += 1
        if self._factory.profile_error is not None:
            raise self._factory.profile_error
        return dict(self._factory.profile)

    async def list_labels(self) -> list[dict[str, Any]]:
        return list(self._factory.labels)

    async def list_messages(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._factory.list_calls.append(kwargs)
        if self._factory.list_error is not None:
            raise self._factory.list_error
        return list(self._factory.listed)

    async def get_message(self, message_id: str) -> dict[str, Any]:
        self._factory.get_calls.append(message_id)
        if self._factory.message_error is not None:
            raise self._factory.message_error
        return dict(self._factory.message)

    async def get_message_metadata(self, message_id: str) -> dict[str, Any]:
        factory = self._factory
        factory.metadata_calls.append(message_id)
        factory.metadata_in_flight += 1
        factory.metadata_max_in_flight = max(
            factory.metadata_max_in_flight, factory.metadata_in_flight
        )
        try:
            # Always yield once so concurrent fetches overlap observably.
            for _ in range(1 + factory.metadata_delays.get(message_id, 0)):
                await asyncio.sleep(0)
            if message_id in factory.metadata_errors:
                raise factory.metadata_errors[message_id]
            if message_id in factory.metadata:
                return dict(factory.metadata[message_id])
            listed = next(item for item in factory.listed if item.get("id") == message_id)
            return dict(listed)
        except asyncio.CancelledError:
            factory.metadata_cancelled.append(message_id)
            raise
        finally:
            factory.metadata_in_flight -= 1

    async def send_message(self, **kwargs: Any) -> dict[str, Any]:
        self._factory.send_calls.append(kwargs)
        if self._factory.send_error is not None:
            raise self._factory.send_error
        return dict(self._factory.sent)

    async def modify_message(self, **kwargs: Any) -> dict[str, Any]:
        self._factory.modify_calls.append(kwargs)
        if self._factory.modify_error is not None:
            raise self._factory.modify_error
        return dict(self._factory.modified)


def _blob(
    *,
    access_token: str = "ya29.stored",
    refresh_token: str | None = "rt-1",
    expires_at: int | None = None,
) -> dict[str, Any]:
    blob: dict[str, Any] = {
        "access_token": access_token,
        "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
    }
    if refresh_token is not None:
        blob["refresh_token"] = refresh_token
    if expires_at is not None:
        blob["expires_at"] = expires_at
    return blob


async def seed_connection(
    db: Database,
    user: CurrentUser,
    *,
    key: str,
    blob: dict[str, Any] | None = None,
    with_blob: bool = True,
) -> None:
    """Insert the user's connected google_mail row with an encrypted blob."""
    if blob is None:
        blob = _blob()
    encrypted = encrypt_credentials(key, json.dumps(blob)) if with_blob else None
    async with db.sessionmaker() as session:
        session.add(
            UserConnection(
                tenant_id=user.tenant_id,
                user_id=user.id,
                module_key="google_mail",
                provider="google",
                status=ConnectionStatus.CONNECTED,
                credentials_encrypted=encrypted,
            )
        )
        await session.commit()


async def seed_scope(db: Database, user: CurrentUser, scope_id: str) -> None:
    """Persist the completed onboarding state carrying the mailbox scope."""
    async with db.sessionmaker() as session:
        session.add(
            OnboardingState(
                tenant_id=user.tenant_id,
                user_id=user.id,
                module_key="google_mail",
                status=OnboardingStatus.COMPLETED,
                current_step=None,
                data={"scopes": [scope_id]},
                secrets_encrypted=None,
            )
        )
        await session.commit()


async def run_tool(
    db: Database,
    user: CurrentUser,
    registry: ToolRegistry,
    tool: str,
    raw_input: dict[str, Any],
    *,
    credentials_key: str | None = None,
    settings: Settings | None = None,
    active_modules: set[str] | None = None,
    approval_grant=None,
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
            active_modules=active_modules if active_modules is not None else {"google_mail"},
            approval_grant=approval_grant,
        )


async def stored_blob(db: Database, user: CurrentUser, key: str) -> dict[str, Any]:
    """Decrypt the user's stored google_mail credentials (test assertions only)."""
    async with db.sessionmaker() as session:
        row = (
            await session.execute(
                select(UserConnection).where(
                    UserConnection.tenant_id == user.tenant_id,
                    UserConnection.user_id == user.id,
                    UserConnection.module_key == "google_mail",
                )
            )
        ).scalar_one()
        assert row.credentials_encrypted is not None
        return json.loads(decrypt_credentials(key, row.credentials_encrypted))


# --- Spec shapes and input validation ------------------------------------------


def test_register_google_mail_tools_registers_six_specs_on_a_local_registry() -> None:
    """Six tools: three free reads, three approval-gated actions. No delete tool."""
    registry = ToolRegistry()

    register_google_mail_tools(registry)

    assert registry.names() == [
        "search_emails",
        "get_email",
        "list_labels",
        "send_email",
        "archive_message",
        "add_label",
    ]
    assert not any("delete" in name or "trash" in name for name in registry.names())


def test_tool_specs_declare_module_roles_kinds_and_policies(mail_registry: ToolRegistry) -> None:
    """Reads never ask for approval; every action is REQUIRE_APPROVAL (criterion 4)."""
    for name in ("search_emails", "get_email", "list_labels"):
        spec = mail_registry.get(name)
        assert (spec.kind, spec.approval_policy, spec.required_role) == (
            ToolKind.EXTRACT,
            ApprovalPolicy.ALLOW_WITHOUT_APPROVAL,
            UserRole.VIEWER,
        )
        assert spec.module == "google_mail"
    for name in ("send_email", "archive_message", "add_label"):
        spec = mail_registry.get(name)
        assert (spec.kind, spec.approval_policy, spec.required_role) == (
            ToolKind.ACTION,
            ApprovalPolicy.REQUIRE_APPROVAL,
            UserRole.OPERATOR,
        )
        assert spec.module == "google_mail"
    assert all(spec.precheck is not None for spec in mail_registry.list_specs())


def test_mailbox_actions_are_decided_only_by_their_requester(mail_registry: ToolRegistry) -> None:
    """G7: each action runs on the requester's own connection; reads need no decision at all."""
    for name in ("search_emails", "get_email", "list_labels"):
        assert mail_registry.get(name).requester_only_approval is False, name
    for name in ("send_email", "archive_message", "add_label"):
        assert mail_registry.get(name).requester_only_approval is True, name


@pytest.mark.parametrize(
    "raw_input",
    [
        {"query": ""},
        {"query": "invoice", "max_results": 0},
        {"query": "invoice", "max_results": 51},
        {"query": "invoice", "extra": "smuggled"},
        {"message_id": ""},
    ],
)
async def test_search_and_get_reject_invalid_input(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, raw_input: dict
) -> None:
    result = await run_tool(
        seeded_db, seeded_users[OWNER], mail_registry, "search_emails", raw_input
    )

    assert (result.status, result.error_kind) == ("error", "validation")
    assert result.error is not None and result.error.startswith("invalid input")


@pytest.mark.parametrize(
    "raw_input",
    [
        {},
        {"to": []},
        {"to": ["not-an-email"], "subject": "Hi", "body": "x"},
        {"to": ["ana@example.com"], "subject": "", "body": "x"},
        {"to": ["ana@example.com"], "subject": "Hi", "body": ""},
        {**SEND_INPUT, "thread_id": ""},
        {**SEND_INPUT, "cc": [""]},
        {**SEND_INPUT, "bcc": [""]},
        {**SEND_INPUT, "extra": 1},
    ],
)
async def test_send_email_rejects_invalid_input(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, raw_input: dict
) -> None:
    result = await run_tool(seeded_db, seeded_users[OWNER], mail_registry, "send_email", raw_input)

    assert (result.status, result.error_kind) == ("error", "validation")


@pytest.mark.parametrize(
    "raw_input",
    [
        {},
        {"message_id": ""},
        {"message_id": "m1", "extra": 1},
        {"message_id": "m1", "label_id": ""},
    ],
)
async def test_message_actions_reject_invalid_input(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, raw_input: dict
) -> None:
    tool = "archive_message" if "label_id" not in raw_input else "add_label"
    result = await run_tool(seeded_db, seeded_users[OWNER], mail_registry, tool, raw_input)

    assert (result.status, result.error_kind) == ("error", "validation")


async def test_list_labels_rejects_extra_input(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry
) -> None:
    result = await run_tool(
        seeded_db, seeded_users[OWNER], mail_registry, "list_labels", {"query": "x"}
    )

    assert (result.status, result.error_kind) == ("error", "validation")


# --- Precheck matrix (criterion 5) ---------------------------------------------


@pytest.mark.parametrize("label_id", ["TRASH", "SPAM", "trash"])
async def test_add_label_refuses_trash_and_spam_labels(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, label_id
) -> None:
    """No delete/trash: applying TRASH or SPAM would trash the message (Gmail purges both)."""
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    register_provider("google", FakeGmail(explode=True))

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "add_label",
        {"message_id": "m1", "label_id": label_id},
        credentials_key=oauth_key,
    )

    assert (result.status, result.error_kind) == ("error", "validation")
    assert result.error is not None and "label_id" in result.error


@pytest.mark.parametrize(("tool", "raw_input"), ALL_TOOLS_AND_INPUTS)
async def test_without_a_selection_prechecks_not_configured(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, tool, raw_input
) -> None:
    """No persisted scope selection (even with a connection) is NOT_CONFIGURED."""
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    register_provider("google", FakeGmail(explode=True))

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        tool,
        raw_input,
        credentials_key=oauth_key,
    )

    assert (result.status, result.error_kind) == ("error", "precheck")
    assert result.error == f"precheck_failed: {NOT_CONFIGURED}"


@pytest.mark.parametrize(("tool", "raw_input"), ALL_TOOLS_AND_INPUTS)
async def test_without_a_connection_prechecks_not_configured(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, tool, raw_input
) -> None:
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    register_provider("google", FakeGmail(explode=True))

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        tool,
        raw_input,
        credentials_key=oauth_key,
    )

    assert result.error == f"precheck_failed: {NOT_CONFIGURED}"


async def test_row_without_credentials_blob_prechecks(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key, with_blob=False)
    register_provider("google", FakeGmail(explode=True))

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        SEARCH_INPUT,
        credentials_key=oauth_key,
    )

    assert result.error == f"precheck_failed: {NOT_CONFIGURED}"


async def test_unreadable_blob_prechecks_without_leaking(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """A blob encrypted under another key is 'not configured', never a 500."""
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=generate_credentials_key())
    register_provider("google", FakeGmail(explode=True))

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        SEARCH_INPUT,
        credentials_key=oauth_key,
    )

    assert (result.status, result.error_kind) == ("error", "precheck")
    assert result.error == f"precheck_failed: {NOT_CONFIGURED}"
    assert oauth_key not in (result.error or "")


async def test_expired_token_refreshes_persists_and_runs(
    seeded_db: Database,
    seeded_users,
    mail_registry: ToolRegistry,
    oauth_key,
    mail_settings,
    monkeypatch,
) -> None:
    """An expired access token is refreshed and the new blob is stored (G2 path)."""
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(
        seeded_db,
        seeded_users[OWNER],
        key=oauth_key,
        blob=_blob(
            access_token="ya29.old", refresh_token="rt-keep", expires_at=int(time.time()) - 120
        ),
    )
    seen: dict[str, Any] = {}

    async def fake_refresh(**kwargs: Any) -> oauth_service.OAuthTokens:
        seen["kwargs"] = kwargs
        return oauth_service.OAuthTokens(
            access_token="ya29.new", refresh_token=None, expires_in=3600, scopes=()
        )

    monkeypatch.setattr(oauth_service, "refresh_access_token", fake_refresh)
    fake = FakeGmail(listed=[{"id": "m2", "labelIds": ["INBOX"]}])
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        SEARCH_INPUT,
        credentials_key=oauth_key,
        settings=mail_settings,
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


@pytest.mark.parametrize(("tool", "raw_input"), ALL_TOOLS_AND_INPUTS)
async def test_expired_token_refresh_failure_prechecks_reconnect(
    seeded_db: Database,
    seeded_users,
    mail_registry: ToolRegistry,
    oauth_key,
    mail_settings,
    monkeypatch,
    tool,
    raw_input,
) -> None:
    """Every tool answers the actionable EXPIRED when the refresh is refused."""

    async def refused_refresh(**kwargs: Any) -> oauth_service.OAuthTokens:
        raise oauth_service.TokenExchangeError("token endpoint answered 400")

    monkeypatch.setattr(oauth_service, "refresh_access_token", refused_refresh)
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(
        seeded_db,
        seeded_users[OWNER],
        key=oauth_key,
        blob=_blob(expires_at=int(time.time()) - 120),
    )
    register_provider("google", FakeGmail(explode=True))

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        tool,
        raw_input,
        credentials_key=oauth_key,
        settings=mail_settings,
    )

    assert (result.status, result.error_kind) == ("error", "precheck")
    assert result.error == f"precheck_failed: {EXPIRED}"
    assert "rt-1" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("tool", "raw_input"),
    [("get_email", GET_INPUT), ("archive_message", ARCHIVE_INPUT), ("add_label", ADD_LABEL_INPUT)],
)
async def test_rejected_token_on_the_scope_check_prechecks_reconnect(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, tool, raw_input
) -> None:
    """A 401/403 while verifying membership is the actionable EXPIRED, never tool_failed."""
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(message_error=GmailAuthenticationError("Gmail API answered 401"))
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        tool,
        raw_input,
        credentials_key=oauth_key,
    )

    assert (result.status, result.error_kind) == ("error", "precheck")
    assert result.error == f"precheck_failed: {EXPIRED}"
    assert fake.modify_calls == []


# --- Scope enforcement (criterion 3, risk #2) ----------------------------------


async def test_search_emails_filters_by_the_selected_label(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(listed=[{"id": "m1", "threadId": "t-1", "labelIds": ["Label_7"]}])
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        SEARCH_INPUT,
        credentials_key=oauth_key,
    )

    assert result.status == "ok"
    assert [message.id for message in result.output.messages] == ["m1"]
    # The scope becomes labelIds, never a query-syntax label (risk #2).
    assert fake.list_calls == [{"label_ids": ["Label_7"], "query": "invoice", "max_results": 5}]


@pytest.mark.parametrize(
    "query",
    [
        "in:anywhere",
        "-label:Label_7",
        "label:INBOX OR label:Label_9",
        "{in:inbox in:spam} is:unread",
    ],
)
async def test_search_query_cannot_widen_the_label_scope(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, query
) -> None:
    """Scope-widening attempt: whatever the query says, labelIds stays the selection.

    Gmail ANDs ``labelIds`` with ``q``, so the model's free text can only
    narrow the selected label, never replace or escape it.
    """
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail()
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        {"query": query},
        credentials_key=oauth_key,
    )

    assert result.status == "ok"
    assert fake.list_calls == [{"label_ids": ["Label_7"], "query": query, "max_results": 10}]


async def test_search_emails_with_all_mail_scope_is_unfiltered(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(
        listed=[{"id": "m1", "threadId": "t-m1"}],
        metadata={"m1": _metadata("m1", label_ids=["INBOX"])},
    )
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        {"max_results": 3},
        credentials_key=oauth_key,
    )

    assert result.status == "ok"
    assert fake.list_calls == [{"label_ids": None, "query": None, "max_results": 3}]
    # ALL skips the per-hit label check: a hit under any label is returned.
    assert [message.id for message in result.output.messages] == ["m1"]


# --- Enriched search (G3.1): one metadata read per hit -------------------------


def _metadata(
    message_id: str,
    *,
    label_ids: list[str],
    subject: str = "Invoice 42",
    sender: str = "ana@example.com",
    date: str = "Fri, 16 Oct 2026 10:00:00 +0200",
    snippet: str = "Please pay invoice 42.",
) -> dict[str, Any]:
    """A ``format=metadata`` Gmail message resource: headers, never a body."""
    return {
        "id": message_id,
        "threadId": f"t-{message_id}",
        "labelIds": label_ids,
        "snippet": snippet,
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": date},
            ],
        },
    }


async def test_search_emails_returns_subject_sender_date_and_snippet_per_hit(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """One search call tells the model what each hit is about (no get_email per hit)."""
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(
        # What users.messagesList really answers: ids only.
        listed=[{"id": "m1", "threadId": "t-m1"}, {"id": "m2", "threadId": "t-m2"}],
        metadata={
            "m1": _metadata("m1", label_ids=["INBOX", "Label_7"]),
            "m2": _metadata(
                "m2",
                label_ids=["Label_7"],
                subject="Lunch",
                sender="Bruno <bruno@example.com>",
                date="Sat, 17 Oct 2026 12:30:00 +0200",
                snippet="Tomorrow at noon?",
            ),
        },
    )
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        SEARCH_INPUT,
        credentials_key=oauth_key,
    )

    assert result.status == "ok"
    assert [message.model_dump() for message in result.output.messages] == [
        {
            "id": "m1",
            "thread_id": "t-m1",
            "label_ids": ["INBOX", "Label_7"],
            "subject": "Invoice 42",
            "sender": "ana@example.com",
            "date": "Fri, 16 Oct 2026 10:00:00 +0200",
            "snippet": "Please pay invoice 42.",
        },
        {
            "id": "m2",
            "thread_id": "t-m2",
            "label_ids": ["Label_7"],
            "subject": "Lunch",
            "sender": "Bruno <bruno@example.com>",
            "date": "Sat, 17 Oct 2026 12:30:00 +0200",
            "snippet": "Tomorrow at noon?",
        },
    ]
    assert fake.metadata_calls == ["m1", "m2"]
    assert fake.get_calls == []  # never the full message (body) for a search hit


async def test_search_emails_keeps_gmail_order_when_metadata_answers_out_of_order(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    ids = ["m1", "m2", "m3", "m4"]
    fake = FakeGmail(
        listed=[{"id": message_id} for message_id in ids],
        metadata={
            message_id: _metadata(message_id, label_ids=["INBOX"], subject=f"Subject {message_id}")
            for message_id in ids
        },
        # m1 answers last and m4 first: the output still follows Gmail's ranking.
        metadata_delays={"m1": 6, "m2": 4, "m3": 2},
    )
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        SEARCH_INPUT,
        credentials_key=oauth_key,
    )

    assert result.status == "ok"
    assert [(message.id, message.subject) for message in result.output.messages] == [
        (message_id, f"Subject {message_id}") for message_id in ids
    ]


async def test_search_emails_fetches_metadata_concurrently_within_the_bound(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """Hits are read concurrently, never more than METADATA_CONCURRENCY at once."""
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(listed=[{"id": f"m{index}"} for index in range(25)])
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        {"max_results": 25},
        credentials_key=oauth_key,
    )

    assert result.status == "ok"
    assert len(result.output.messages) == 25
    assert 1 < METADATA_CONCURRENCY < 25
    assert fake.metadata_max_in_flight == METADATA_CONCURRENCY


async def test_search_emails_skips_a_hit_deleted_between_list_and_get(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """A 404 on one metadata read drops that hit; the search still succeeds."""
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(
        listed=[{"id": "m1"}, {"id": "m2"}, {"id": "m3"}],
        metadata={
            message_id: _metadata(message_id, label_ids=["Label_7"])
            for message_id in ("m1", "m2", "m3")
        },
        metadata_errors={"m2": GmailNotFoundError("Gmail API answered 404")},
    )
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        SEARCH_INPUT,
        credentials_key=oauth_key,
    )

    assert result.status == "ok"
    assert [message.id for message in result.output.messages] == ["m1", "m3"]


async def test_search_emails_drops_a_hit_whose_labels_miss_the_scope(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """Defense in depth: labelIds filters the list, and the metadata re-checks each hit.

    Gmail should never list a message outside ``labelIds``; if it ever
    does (or the label moved between list and get), the hit is dropped.
    """
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(
        listed=[{"id": "m1"}, {"id": "m2"}],
        metadata={
            "m1": _metadata("m1", label_ids=["INBOX", "Label_7"]),
            "m2": _metadata("m2", label_ids=["INBOX"], subject="Outside the scope"),
        },
    )
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        SEARCH_INPUT,
        credentials_key=oauth_key,
    )

    assert result.status == "ok"
    assert [message.id for message in result.output.messages] == ["m1"]
    assert "Outside the scope" not in result.model_dump_json()


@pytest.mark.parametrize("status", [401, 403])
async def test_search_emails_rejected_token_on_a_metadata_read_fails_masked(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, status
) -> None:
    """A 401/403 on any hit aborts the whole search: no partial page, no token leak.

    The executor masks every handler exception as ``tool_failed`` (only a
    precheck can answer the ``EXPIRED`` reason), exactly like a 401 on the
    list call itself.
    """
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(
        listed=[{"id": "m1"}, {"id": "m2"}],
        metadata={"m1": _metadata("m1", label_ids=["Label_7"])},
        metadata_errors={"m2": GmailAuthenticationError(f"Gmail API answered {status}")},
    )
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        SEARCH_INPUT,
        credentials_key=oauth_key,
    )

    assert (result.status, result.error, result.error_kind) == ("error", "tool_failed", "execution")
    assert result.output is None
    assert "ya29" not in result.model_dump_json()


async def test_search_emails_failure_cancels_the_pending_metadata_reads(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """An aborted search leaves no read running against Gmail in the background."""
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(
        listed=[{"id": "m1"}, {"id": "m2"}],
        metadata_errors={"m1": GmailProviderError("Gmail API answered 500")},
        metadata_delays={"m2": 1_000},  # still pending when m1 fails
    )
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        SEARCH_INPUT,
        credentials_key=oauth_key,
    )
    await asyncio.sleep(0)  # let the cancellation reach the pending read

    assert result.error == "tool_failed"
    assert fake.metadata_cancelled == ["m2"]


@pytest.mark.parametrize(
    "error",
    [GmailProviderError("Gmail API answered 500"), httpx.ConnectError("connection refused")],
)
async def test_search_emails_metadata_failure_is_a_masked_tool_failure(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, error
) -> None:
    """Any other metadata failure follows the module mapping: never a 500, never a leak."""
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(listed=[{"id": "m1"}, {"id": "m2"}], metadata_errors={"m1": error})
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        SEARCH_INPUT,
        credentials_key=oauth_key,
    )

    assert (result.status, result.error, result.error_kind) == ("error", "tool_failed", "execution")
    assert "ya29" not in result.model_dump_json()


def test_search_emails_description_says_hits_are_already_described() -> None:
    """The model learns search answers subject/sender/date/snippet; get_email is the body."""
    description = SEARCH_EMAILS.description.lower()

    for field in ("subject", "sender", "date", "snippet"):
        assert field in description, field
    assert "get_email" in description
    assert "full body" in description


async def test_get_email_returns_a_message_inside_the_scope(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail()
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "get_email",
        GET_INPUT,
        credentials_key=oauth_key,
    )

    assert result.status == "ok"
    message = result.output.message
    assert (message.id, message.thread_id) == ("m1", "t-1")
    assert message.label_ids == ["INBOX", "Label_7"]
    assert message.subject == "Invoice 42"
    assert message.sender == "ana@example.com"
    assert message.to == ["me@acme.test"]
    assert message.date == "Fri, 16 Oct 2026 10:00:00 +0200"
    assert message.body == "Please pay invoice 42."
    # One fetch in the scope precheck, one in the handler (the frozen
    # ToolContext cannot pass the message through, and the precheck returns None).
    assert fake.get_calls == ["m1", "m1"]


async def test_get_email_denies_a_message_outside_the_scope(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """Criterion 3's heart: out-of-scope read is denied with an actionable reason."""
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(message=OUT_OF_SCOPE_MESSAGE)
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "get_email",
        GET_INPUT,
        credentials_key=oauth_key,
    )

    assert (result.status, result.error_kind) == ("error", "precheck")
    assert result.error == f"precheck_failed: {OUT_OF_SCOPE}"


async def test_get_email_with_all_mail_scope_reads_any_message(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """ALL skips the label-membership check entirely."""
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(message=OUT_OF_SCOPE_MESSAGE)
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "get_email",
        GET_INPUT,
        credentials_key=oauth_key,
    )

    assert result.status == "ok"
    assert result.output.message.label_ids == ["INBOX"]


# --- get_email bodies: text/plain first, then readable HTML (G8) -----------------


def _b64(text: str) -> str:
    """base64url of one MIME part body, as Gmail's ``format=full`` carries it."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _part(mime_type: str, text: str) -> dict[str, Any]:
    return {"mimeType": mime_type, "body": {"data": _b64(text)}}


def _message_with(payload: dict[str, Any]) -> dict[str, Any]:
    """``IN_SCOPE_MESSAGE`` with another MIME tree under the same headers."""
    return {
        **IN_SCOPE_MESSAGE,
        "payload": {"headers": IN_SCOPE_MESSAGE["payload"]["headers"], **payload},
    }


#: A typical HTML-only mail: head/style/script noise around block-level text.
NEWSLETTER_HTML = (
    "<html><head><title>Hidden title</title><style>p { color: red; }</style></head>"
    "<body><h1>Invoice&nbsp;42</h1>\n<p>Dear   Ana,<br>please pay &amp; reply.</p>"
    "<script>alert('x')</script><div>Items:</div><ul><li>Hosting</li>\n  <li>Support</li></ul>"
    "<table><tr><td>Total</td><td>&euro;10</td></tr></table></body></html>"
)


async def _read_body(
    db: Database, user: CurrentUser, registry: ToolRegistry, key: str, message: dict[str, Any]
) -> str | None:
    """Run ``get_email`` on ``message`` (whole-mailbox scope) and return its body."""
    await seed_scope(db, user, ALL_SCOPE)
    await seed_connection(db, user, key=key)
    register_provider("google", FakeGmail(message=message))
    result = await run_tool(db, user, registry, "get_email", GET_INPUT, credentials_key=key)
    assert result.status == "ok", result.error
    return result.output.message.body


async def test_get_email_converts_an_html_only_body_to_readable_text(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """No text/plain part: the HTML reaches the model as text, not tags or script."""
    body = await _read_body(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        oauth_key,
        _message_with(_part("text/html", NEWSLETTER_HTML)),
    )

    assert body == (
        "Invoice 42\n\nDear Ana,\nplease pay & reply.\n\nItems:\nHosting\nSupport\nTotal €10"
    )
    for noise in ("<", ">", "alert", "color", "Hidden title", "&amp;", "&nbsp;"):
        assert noise not in body


async def test_get_email_finds_a_nested_html_only_part(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """multipart/mixed → multipart/alternative → text/html, next to an attachment."""
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [_part("text/html", "<p>Hello Ana</p>")],
            },
            {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"attachmentId": "x"}},
        ],
    }

    body = await _read_body(
        seeded_db, seeded_users[OWNER], mail_registry, oauth_key, _message_with(payload)
    )

    assert body == "Hello Ana"


async def test_get_email_prefers_text_plain_over_html(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """Both alternatives present: text/plain wins, even when the HTML part comes first."""
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            _part("text/html", "<p>HTML version</p>"),
            _part("text/plain", "Plain version"),
        ],
    }

    body = await _read_body(
        seeded_db, seeded_users[OWNER], mail_registry, oauth_key, _message_with(payload)
    )

    assert body == "Plain version"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<head><title>T</title><body><p>Hi</p>", "Hi"),  # unclosed head
        ("<p>A&nbsp;&amp;&#39;B&#x27;</p>", "A &'B'"),
        ("Line one<br>Line two<br><br>Line four", "Line one\nLine two\n\nLine four"),
        ("<!-- hidden --><b>Bold</b> <i>text</i>", "Bold text"),
        ("<html><head><style>x</style></head><body><img src='a.png'></body></html>", None),
    ],
    ids=["unclosed-head", "entities", "line-breaks", "inline-and-comment", "no-text"],
)
async def test_get_email_html_conversion_edge_cases(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, html, expected
) -> None:
    body = await _read_body(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        oauth_key,
        _message_with(_part("text/html", html)),
    )

    assert body == expected


async def test_get_email_caps_the_converted_html_body(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, monkeypatch
) -> None:
    """The MAX_DECODED_BODY_CHARS limit holds for converted HTML too."""
    monkeypatch.setattr(mail_tools, "MAX_DECODED_BODY_CHARS", 12)

    body = await _read_body(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        oauth_key,
        _message_with(_part("text/html", "<p>" + "x" * 100 + "</p>")),
    )

    assert body == "x" * 12


async def test_archive_message_inside_the_scope_removes_inbox(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail()
    register_provider("google", fake)
    grant = mint_grant(
        uuid4(),
        compute_action_key("archive_message", ArchiveMessageInput.model_validate(ARCHIVE_INPUT)),
    )

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "archive_message",
        ARCHIVE_INPUT,
        credentials_key=oauth_key,
        approval_grant=grant,
    )

    assert result.status == "ok"
    assert (result.output.archived, result.output.message_id) == (True, "m1")
    # The precheck's verification fetch plus the handler's modify.
    assert fake.get_calls == ["m1"]
    assert fake.modify_calls == [
        {"message_id": "m1", "add_label_ids": None, "remove_label_ids": ["INBOX"]}
    ]


async def test_archive_message_outside_the_scope_is_denied_before_any_modify(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """Criterion 3: no archive of a message outside the selected label."""
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(message=OUT_OF_SCOPE_MESSAGE)
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "archive_message",
        ARCHIVE_INPUT,
        credentials_key=oauth_key,
    )

    assert (result.status, result.error_kind) == ("error", "precheck")
    assert result.error == f"precheck_failed: {OUT_OF_SCOPE}"
    assert fake.modify_calls == []


async def test_add_label_inside_the_scope_applies_any_label(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """The CONSTRAINT is the message; the label itself may be any label."""
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail()
    register_provider("google", fake)
    grant = mint_grant(
        uuid4(),
        compute_action_key("add_label", AddLabelInput.model_validate(ADD_LABEL_INPUT)),
    )

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "add_label",
        ADD_LABEL_INPUT,
        credentials_key=oauth_key,
        approval_grant=grant,
    )

    assert result.status == "ok"
    assert (result.output.labeled, result.output.label_id) == (True, "Label_9")
    assert fake.modify_calls == [
        {"message_id": "m1", "add_label_ids": ["Label_9"], "remove_label_ids": None}
    ]


async def test_add_label_outside_the_scope_is_denied_before_any_modify(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(message=OUT_OF_SCOPE_MESSAGE)
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "add_label",
        ADD_LABEL_INPUT,
        credentials_key=oauth_key,
    )

    assert (result.status, result.error_kind) == ("error", "precheck")
    assert result.error == f"precheck_failed: {OUT_OF_SCOPE}"
    assert fake.modify_calls == []


async def test_send_email_is_exempt_from_the_label_check(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """A new message belongs to no folder: sending works under a label scope."""
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail()
    register_provider("google", fake)
    grant = mint_grant(
        uuid4(), compute_action_key("send_email", SendEmailInput.model_validate(SEND_INPUT))
    )

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "send_email",
        SEND_INPUT,
        credentials_key=oauth_key,
        approval_grant=grant,
    )

    assert result.status == "ok"
    assert (result.output.id, result.output.thread_id) == ("sent-1", "t-9")
    assert fake.get_calls == []  # no label membership fetch for send
    assert len(fake.send_calls) == 1


# --- Approval gate (criterion 4) -----------------------------------------------


@pytest.mark.parametrize(("tool", "raw_input"), ACTION_TOOLS_AND_INPUTS)
async def test_action_without_grant_is_approval_required_and_never_calls_gmail(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, tool, raw_input
) -> None:
    """No approval → approval_required, and the provider is never touched."""
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail()
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        tool,
        raw_input,
        credentials_key=oauth_key,
    )

    assert result.status == "approval_required"
    payload = result.approval_request
    assert payload is not None
    assert payload.tool_name == tool
    assert payload.required_role == "operator"
    assert fake.send_calls == []
    assert fake.modify_calls == []


@pytest.mark.parametrize(("tool", "raw_input"), ACTION_TOOLS_AND_INPUTS)
async def test_action_with_minted_grant_executes(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, tool, raw_input
) -> None:
    """A live grant bound to this tool+input runs the handler exactly once."""
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail()
    register_provider("google", fake)
    input_model = {
        "send_email": SendEmailInput,
        "archive_message": ArchiveMessageInput,
        "add_label": AddLabelInput,
    }[tool]
    grant = mint_grant(uuid4(), compute_action_key(tool, input_model.model_validate(raw_input)))

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        tool,
        raw_input,
        credentials_key=oauth_key,
        approval_grant=grant,
    )

    assert result.status == "ok"
    assert len(fake.send_calls) + len(fake.modify_calls) == 1


async def test_granted_archive_of_an_out_of_scope_message_is_still_refused(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """The precheck re-runs before the approved handler: approval never widens scope."""
    await seed_scope(seeded_db, seeded_users[OWNER], "Label_7")
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(message=OUT_OF_SCOPE_MESSAGE)
    register_provider("google", fake)
    grant = mint_grant(
        uuid4(),
        compute_action_key("archive_message", ArchiveMessageInput.model_validate(ARCHIVE_INPUT)),
    )

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "archive_message",
        ARCHIVE_INPUT,
        credentials_key=oauth_key,
        approval_grant=grant,
    )

    assert (result.status, result.error_kind) == ("error", "precheck")
    assert result.error == f"precheck_failed: {OUT_OF_SCOPE}"
    assert fake.modify_calls == []


async def test_grant_for_another_input_is_refused(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail()
    register_provider("google", fake)
    grant = mint_grant(
        uuid4(), compute_action_key("send_email", SendEmailInput.model_validate(SEND_INPUT))
    )

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "send_email",
        {**SEND_INPUT, "subject": "Different"},
        credentials_key=oauth_key,
        approval_grant=grant,
    )

    assert (result.status, result.error_kind) == ("error", "forbidden")
    assert fake.send_calls == []


async def test_read_tools_execute_without_any_grant(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """Criterion 4's other half: reading never asks for approval."""
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail()
    register_provider("google", fake)

    for tool, raw_input in READ_TOOLS_AND_INPUTS:
        result = await run_tool(
            seeded_db,
            seeded_users[OWNER],
            mail_registry,
            tool,
            raw_input,
            credentials_key=oauth_key,
        )
        assert result.status == "ok", tool


async def test_approved_archive_executes_through_the_approvals_service(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, mail_settings
) -> None:
    """Criterion 4 end to end: request → human approval → approvals execute → Gmail modify."""
    owner = seeded_users[OWNER]
    await seed_scope(seeded_db, owner, "Label_7")
    await seed_connection(seeded_db, owner, key=oauth_key)
    fake = FakeGmail()
    register_provider("google", fake)
    async with seeded_db.sessionmaker() as session:
        session.add(ModuleActivation(tenant_id=owner.tenant_id, module_key="google_mail"))
        request = await create_approval_request(
            session, mail_registry, owner, "archive_message", ARCHIVE_INPUT, settings=mail_settings
        )
        await session.commit()
        request_id = request.id
    assert fake.modify_calls == []  # pending: nothing ran yet
    async with seeded_db.sessionmaker() as session:
        await approve_request(session, owner, request_id)

    async with seeded_db.sessionmaker() as session:
        execution = await execute_approved_action(
            session,
            mail_registry,
            owner,
            request_id,
            credentials_key=oauth_key,
            settings=mail_settings,
        )

    assert execution.result.status == "ok", execution.result.error
    assert fake.modify_calls == [
        {"message_id": "m1", "add_label_ids": None, "remove_label_ids": ["INBOX"]}
    ]


async def test_another_user_cannot_decide_or_execute_a_mailbox_action(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, mail_settings
) -> None:
    """G7: an admin of the same tenant cannot approve, reject, or run the owner's send."""
    owner, admin = seeded_users[OWNER], seeded_users[ADMIN]
    for user in (owner, admin):
        await seed_scope(seeded_db, user, ALL_SCOPE)
        await seed_connection(seeded_db, user, key=oauth_key)
    fake = FakeGmail()
    register_provider("google", fake)
    async with seeded_db.sessionmaker() as session:
        session.add(ModuleActivation(tenant_id=owner.tenant_id, module_key="google_mail"))
        request = await create_approval_request(
            session, mail_registry, owner, "send_email", SEND_INPUT, settings=mail_settings
        )
        await session.commit()
        request_id = request.id

    async with seeded_db.sessionmaker() as session:
        with pytest.raises(ApprovalForbiddenError):
            await approve_request(session, admin, request_id, registry=mail_registry)
        with pytest.raises(ApprovalForbiddenError):
            await reject_request(session, admin, request_id, registry=mail_registry)
        await approve_request(session, owner, request_id, registry=mail_registry)
    async with seeded_db.sessionmaker() as session:
        with pytest.raises(ApprovalForbiddenError):
            await execute_approved_action(
                session,
                mail_registry,
                admin,
                request_id,
                credentials_key=oauth_key,
                settings=mail_settings,
            )
    assert fake.send_calls == []

    async with seeded_db.sessionmaker() as session:
        execution = await execute_approved_action(
            session,
            mail_registry,
            owner,
            request_id,
            credentials_key=oauth_key,
            settings=mail_settings,
        )

    assert execution.result.status == "ok", execution.result.error
    assert execution.request.executed_by == owner.id
    assert len(fake.send_calls) == 1


# --- send_email MIME (RFC 822 via stdlib) ---------------------------------------


async def test_send_email_builds_rfc822_mime_and_passes_thread_id(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail()
    register_provider("google", fake)
    raw_input = {
        "to": ["ana@example.com", "bruno@example.com"],
        "cc": ["caro@example.com"],
        "bcc": ["dan@example.com"],
        "subject": "Invoice 42",
        "body": "Please pay invoice 42.",
        "thread_id": "t-9",
    }
    grant = mint_grant(
        uuid4(), compute_action_key("send_email", SendEmailInput.model_validate(raw_input))
    )

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "send_email",
        raw_input,
        credentials_key=oauth_key,
        approval_grant=grant,
    )

    assert result.status == "ok"
    [call] = fake.send_calls
    assert call["thread_id"] == "t-9"
    padded = call["raw_mime_b64url"] + "=" * (-len(call["raw_mime_b64url"]) % 4)
    mime = message_from_bytes(base64.urlsafe_b64decode(padded), policy=default)
    assert mime["To"] == "ana@example.com, bruno@example.com"
    assert mime["Cc"] == "caro@example.com"
    assert mime["Bcc"] == "dan@example.com"
    assert mime["Subject"] == "Invoice 42"
    assert mime.get_content().strip() == "Please pay invoice 42."


async def test_send_email_omits_thread_id_when_not_replying(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail()
    register_provider("google", fake)
    grant = mint_grant(
        uuid4(), compute_action_key("send_email", SendEmailInput.model_validate(SEND_INPUT))
    )

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "send_email",
        SEND_INPUT,
        credentials_key=oauth_key,
        approval_grant=grant,
    )

    assert result.status == "ok"
    [call] = fake.send_calls
    assert call["thread_id"] is None


# --- send_email sender headers and delivery confirmation (G8) -------------------

#: The connected mailbox's address as ``users.getProfile`` answers it.
SENDER = "owner.mailbox@acme.test"


def _sent_mime(raw_mime_b64url: str) -> Any:
    """Decode the base64url raw MIME one ``users.messagesSend`` call carried."""
    padded = raw_mime_b64url + "=" * (-len(raw_mime_b64url) % 4)
    return message_from_bytes(base64.urlsafe_b64decode(padded), policy=default)


def _send_grant(raw_input: dict[str, Any] | None = None):
    validated = SendEmailInput.model_validate(raw_input if raw_input is not None else SEND_INPUT)
    return mint_grant(uuid4(), compute_action_key("send_email", validated))


async def _seed_mailbox(db: Database, user: CurrentUser, key: str) -> None:
    await seed_scope(db, user, ALL_SCOPE)
    await seed_connection(db, user, key=key)


async def test_send_email_sets_from_to_the_connected_address_with_date_and_message_id(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """The observed "(unknown sender)": From is the users.getProfile address, never omitted."""
    await _seed_mailbox(seeded_db, seeded_users[OWNER], oauth_key)
    fake = FakeGmail(profile={"emailAddress": SENDER, "messagesTotal": 3})
    register_provider("google", fake)
    before = datetime.now(UTC).replace(microsecond=0)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "send_email",
        SEND_INPUT,
        credentials_key=oauth_key,
        approval_grant=_send_grant(),
    )

    after = datetime.now(UTC)
    assert result.status == "ok", result.error
    [call] = fake.send_calls
    mime = _sent_mime(call["raw_mime_b64url"])
    assert mime["From"] == SENDER
    assert mime["To"] == "ana@example.com"
    sent_at = parsedate_to_datetime(mime["Date"])
    assert sent_at.tzinfo is not None
    assert before <= sent_at <= after
    assert re.fullmatch(r"<[^<>@\s]+@acme\.test>", mime["Message-ID"])


@pytest.mark.parametrize("granted", [True, False], ids=["approved-execution", "request"])
async def test_send_email_rejected_profile_read_prechecks_reconnect_and_sends_nothing(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, granted
) -> None:
    """A 401/403 on users.getProfile is the actionable EXPIRED, before approval and send."""
    await _seed_mailbox(seeded_db, seeded_users[OWNER], oauth_key)
    fake = FakeGmail(profile_error=GmailAuthenticationError("Gmail API answered 401"))
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "send_email",
        SEND_INPUT,
        credentials_key=oauth_key,
        approval_grant=_send_grant() if granted else None,
    )

    assert (result.status, result.error_kind) == ("error", "precheck")
    assert result.error == f"precheck_failed: {EXPIRED}"
    assert fake.send_calls == []


@pytest.mark.parametrize(
    "profile_error",
    [
        GmailProviderError("Gmail API answered 500"),
        httpx.ConnectError("refused", request=httpx.Request("GET", GOOGLE_GMAIL_PROFILE_URL)),
    ],
    ids=["provider-error", "transport-error"],
)
async def test_send_email_unreadable_profile_is_masked_and_sends_nothing(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, profile_error
) -> None:
    await _seed_mailbox(seeded_db, seeded_users[OWNER], oauth_key)
    fake = FakeGmail(profile_error=profile_error)
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "send_email",
        SEND_INPUT,
        credentials_key=oauth_key,
        approval_grant=_send_grant(),
    )

    assert (result.status, result.error, result.error_kind) == ("error", "tool_failed", "execution")
    assert "ya29" not in result.model_dump_json()
    assert fake.send_calls == []


@pytest.mark.parametrize(
    "profile",
    [
        {},
        {"emailAddress": ""},
        {"emailAddress": 7},
        {"emailAddress": "not-an-address"},
        {"emailAddress": "@acme.test"},
        {"emailAddress": "a@b@acme.test"},
        {"emailAddress": " agent@acme.test"},
        {"emailAddress": "Agent <agent@acme.test>"},
        {"emailAddress": "agent@acme.test\r\nBcc: spy@evil.test"},
    ],
    ids=[
        "missing",
        "empty",
        "not-a-string",
        "no-at",
        "no-local-part",
        "two-ats",
        "padded",
        "display-name",
        "header-injection",
    ],
)
async def test_send_email_without_a_usable_sender_address_sends_nothing(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, profile
) -> None:
    """Never guess or splice a sender: an unusable profile address is a masked failure."""
    await _seed_mailbox(seeded_db, seeded_users[OWNER], oauth_key)
    fake = FakeGmail(profile=profile)
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "send_email",
        SEND_INPUT,
        credentials_key=oauth_key,
        approval_grant=_send_grant(),
    )

    assert (result.status, result.error, result.error_kind) == ("error", "tool_failed", "execution")
    assert fake.send_calls == []


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        (401, f"precheck_failed: {EXPIRED}"),
        (403, f"precheck_failed: {EXPIRED}"),
        (500, "tool_failed"),
    ],
)
async def test_send_email_never_posts_messages_send_when_the_profile_read_fails(
    seeded_db: Database,
    seeded_users,
    mail_registry: ToolRegistry,
    oauth_key,
    status,
    expected_error,
) -> None:
    """Wire level (real adapter): a failed users.getProfile means no messages/send request."""
    await _seed_mailbox(seeded_db, seeded_users[OWNER], oauth_key)
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if str(request.url) == GOOGLE_GMAIL_PROFILE_URL:
            return httpx.Response(status, json={"error": {"code": status}})
        return httpx.Response(200, json={"id": "sent-1", "labelIds": ["SENT"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        register_provider(
            "google", lambda access_token: GoogleGmailProvider(access_token, client=client)
        )
        result = await run_tool(
            seeded_db,
            seeded_users[OWNER],
            mail_registry,
            "send_email",
            SEND_INPUT,
            credentials_key=oauth_key,
            approval_grant=_send_grant(),
        )

    assert result.error == expected_error
    assert seen, "the profile must be read before any send"
    assert all(url == GOOGLE_GMAIL_PROFILE_URL for _, url in seen)
    assert ("POST", GOOGLE_GMAIL_MESSAGES_URL) not in seen


async def test_send_email_posts_mime_from_the_profile_address_over_the_wire(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """Wire level (real adapter): the posted raw MIME carries From, threadId passes through."""
    await _seed_mailbox(seeded_db, seeded_users[OWNER], oauth_key)
    raw_input = {**SEND_INPUT, "thread_id": "t-9"}
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and str(request.url) == GOOGLE_GMAIL_PROFILE_URL:
            return httpx.Response(200, json={"emailAddress": SENDER, "messagesTotal": 3})
        if request.method == "POST" and str(request.url) == GOOGLE_GMAIL_MESSAGES_URL:
            posted.append(json.loads(request.content))
            return httpx.Response(
                200, json={"id": "sent-1", "threadId": "t-9", "labelIds": ["SENT"]}
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        register_provider(
            "google", lambda access_token: GoogleGmailProvider(access_token, client=client)
        )
        result = await run_tool(
            seeded_db,
            seeded_users[OWNER],
            mail_registry,
            "send_email",
            raw_input,
            credentials_key=oauth_key,
            approval_grant=_send_grant(raw_input),
        )

    assert result.status == "ok", result.error
    assert (result.output.id, result.output.thread_id) == ("sent-1", "t-9")
    [body] = posted
    assert body["threadId"] == "t-9"
    mime = _sent_mime(body["raw"])
    assert mime["From"] == SENDER
    assert mime["Date"] and mime["Message-ID"]


@pytest.mark.parametrize(
    "labels", [["SENT"], ["SENT", "INBOX", "UNREAD"]], ids=["sent", "sent-to-self"]
)
async def test_send_email_labelled_sent_is_reported_as_sent(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, labels
) -> None:
    await _seed_mailbox(seeded_db, seeded_users[OWNER], oauth_key)
    fake = FakeGmail(sent={"id": "sent-1", "threadId": "t-9", "labelIds": labels})
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "send_email",
        SEND_INPUT,
        credentials_key=oauth_key,
        approval_grant=_send_grant(),
    )

    assert result.status == "ok", result.error
    assert result.output.id == "sent-1"


@pytest.mark.parametrize(
    "sent",
    [
        {"id": "sent-1", "threadId": "t-9"},
        {"id": "sent-1", "threadId": "t-9", "labelIds": []},
        {"id": "sent-1", "threadId": "t-9", "labelIds": ["INBOX"]},
        {"id": "sent-1", "threadId": "t-9", "labelIds": "SENT"},
    ],
    ids=["labels-absent", "labels-empty", "labels-without-sent", "labels-not-a-list"],
)
async def test_send_email_without_the_sent_label_is_not_reported_as_sent(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key, sent, caplog
) -> None:
    """Observed on 1a1532e2dfb00a3d: no labelIds at all — never claim that mail was sent."""
    await _seed_mailbox(seeded_db, seeded_users[OWNER], oauth_key)
    fake = FakeGmail(sent=sent)
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "send_email",
        SEND_INPUT,
        credentials_key=oauth_key,
        approval_grant=_send_grant(),
    )

    assert (result.status, result.error, result.error_kind) == ("error", "tool_failed", "execution")
    assert "ya29" not in result.model_dump_json()
    assert len(fake.send_calls) == 1
    # The masked failure keeps its reason server-side (log + audit), never for the caller.
    assert "Gmail did not mark the message as sent" in caplog.text


# --- list_labels and failure mapping --------------------------------------------


async def test_list_labels_returns_id_and_name_pairs(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(
        labels=[
            {"id": "INBOX", "name": "Inbox", "type": "system"},
            {"id": "Label_7", "name": "Asistente", "type": "user"},
            {"type": "user"},  # malformed entry: skipped
        ]
    )
    register_provider("google", fake)

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "list_labels",
        {},
        credentials_key=oauth_key,
    )

    assert result.status == "ok"
    assert [(label.id, label.name) for label in result.output.labels] == [
        ("INBOX", "Inbox"),
        ("Label_7", "Asistente"),
    ]


async def test_provider_failure_is_a_masked_tool_failure(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    """A mid-call Gmail failure never 500s and never leaks the token."""
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    fake = FakeGmail(send_error=GmailProviderError("Gmail API answered 500"))
    register_provider("google", fake)
    grant = mint_grant(
        uuid4(), compute_action_key("send_email", SendEmailInput.model_validate(SEND_INPUT))
    )

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "send_email",
        SEND_INPUT,
        credentials_key=oauth_key,
        approval_grant=grant,
    )

    assert (result.status, result.error, result.error_kind) == ("error", "tool_failed", "execution")
    assert "ya29" not in result.model_dump_json()


# --- Executor integration -------------------------------------------------------


async def test_inactive_module_is_refused_before_the_precheck(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    await seed_scope(seeded_db, seeded_users[OWNER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[OWNER], key=oauth_key)
    register_provider("google", FakeGmail(explode=True))

    result = await run_tool(
        seeded_db,
        seeded_users[OWNER],
        mail_registry,
        "search_emails",
        SEARCH_INPUT,
        credentials_key=oauth_key,
        active_modules=set(),
    )

    assert (result.status, result.error, result.error_kind) == (
        "error",
        "inactive_module",
        "inactive_module",
    )


async def test_viewer_may_not_invoke_actions(
    seeded_db: Database, seeded_users, mail_registry: ToolRegistry, oauth_key
) -> None:
    await seed_scope(seeded_db, seeded_users[VIEWER], ALL_SCOPE)
    await seed_connection(seeded_db, seeded_users[VIEWER], key=oauth_key)
    register_provider("google", FakeGmail(explode=True))

    for tool, raw_input in ACTION_TOOLS_AND_INPUTS:
        result = await run_tool(
            seeded_db,
            seeded_users[VIEWER],
            mail_registry,
            tool,
            raw_input,
            credentials_key=oauth_key,
        )
        assert (result.status, result.error, result.error_kind) == (
            "error",
            "forbidden",
            "forbidden",
        )


def test_precheck_reasons_are_distinct_and_actionable() -> None:
    """Three separate English reasons the model can relay to the user."""
    reasons = {NOT_CONFIGURED, EXPIRED, OUT_OF_SCOPE}
    assert len(reasons) == 3
    for reason in reasons:
        assert reason.strip()
        assert reason == reason.strip()
        assert "Settings > Tools" in reason


def test_precheck_error_carries_the_out_of_scope_reason() -> None:
    """The denial reason reaches the user through the precheck error shape."""
    error = ToolPrecheckError(OUT_OF_SCOPE)

    assert error.reason == OUT_OF_SCOPE
