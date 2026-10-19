"""Calendar chat tools (D5/D6, T3): list, create, and cancel the user's events.

Three ToolSpecs on module ``calendar``, registered through
:func:`register_calendar_tools`: ``app/main.py`` calls it once at import time
on the process-wide ``default_registry`` (human-approved allowlist entry,
locked by ``tests/test_tools_executor.py``); tests call it against a LOCAL
:class:`~app.modules.tools.registry.ToolRegistry`.

Tool shapes (D5 — all ``ALLOW_WITHOUT_APPROVAL``: this is the user's own
calendar and the action is self-service, so the model never waits for a human
approval round-trip):

- ``list_events`` — ``EXTRACT`` / viewer: events of one calendar (default
  ``primary``) inside a required ``from``/``to`` range, bounded
  ``max_results`` (1–50, default 10);
- ``create_event`` — ``ACTION`` / operator: insert one event
  (``dateTime`` ISO 8601; all-day events are out of the MVP);
- ``cancel_event`` — ``ACTION`` / operator: delete one event by id.

Times follow the user's timezone (agent-engine-routing D7): a datetime input
may be naive — the user's wall-clock time — or carry an offset, never both
in one input. Handlers localize naive values with ``ToolContext.timezone``
(UTC when it is ``None`` or unusable) and pass that zone name to the provider
as ``time_zone``, so Google stores and returns times in the user's zone.

``operator`` (not ``viewer``) gates the two ACTIONs because viewer is the
read-only role in this repo's rank map — an operator is the lowest role that
may change state anywhere else too.

Shared precheck (never a 500, never leaks the blob or tokens): the user's
newest ``calendar`` connection must exist with a readable credentials blob,
and its access token must be usable. An expired token is refreshed on demand
(D3): the precheck calls the oauth service's ``refresh_access_token`` and
persists the new blob through
:func:`~app.modules.connections.service.create_or_update` (same
provider/module — the upsert keeps the row and flips it back to
``connected``); a refused or impossible refresh answers the "reconnect"
reason. Every failure is a :class:`~app.modules.tools.exceptions.ToolPrecheckError`
with a short, English, actionable reason the model can relay to the user.

The precheck loads and validates the connection but never builds a provider;
only the handlers do, so a refused call can never touch the external API.
"""

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import (
    CredentialsKeyFormatError,
    DecryptionError,
    decrypt_credentials,
)
from app.modules.calendar.manifest import CALENDAR_MANIFEST
from app.modules.calendar.provider import CalendarProvider, get_provider
from app.modules.catalog.registry import ModuleCatalog
from app.modules.connections.models import UserConnection
from app.modules.connections.service import create_or_update
from app.modules.identity.models import UserRole
from app.modules.oauth import service as oauth_service
from app.modules.tools.exceptions import ToolPrecheckError
from app.modules.tools.registry import (
    ApprovalPolicy,
    ToolContext,
    ToolKind,
    ToolRegistry,
    ToolSpec,
)

logger = logging.getLogger(__name__)

MODULE_KEY = "calendar"

#: Precheck reasons (short, English, actionable — the model relays them).
NOT_CONFIGURED = (
    "calendar connection not configured; the user must connect Google in Settings > Tools first"
)
EXPIRED = "calendar connection expired; the user must reconnect Google in Settings > Tools"

#: Treat a token as expired this many seconds early (clock skew allowance).
EXPIRY_SKEW_SECONDS = 60

MAX_RESULTS_DEFAULT = 10
MAX_RESULTS_LIMIT = 50
MAX_ATTENDEES = 50
MAX_SUMMARY_LENGTH = 1024
MAX_DESCRIPTION_LENGTH = 4096


#: Sentence appended to the time-taking tool descriptions (sent to the model on every call).
TIMES_NOTE = (
    "Times without an offset are read in the user's timezone (stated in the system "
    "message); an offset is optional."
)


def _is_naive(value: datetime) -> bool:
    """True for a wall-clock datetime without a UTC offset."""
    return value.utcoffset() is None


def _same_form(first: datetime, second: datetime, fields: str) -> None:
    """Reject one input that mixes a naive datetime with an offset-bearing one.

    Called before any ordering check, so ``<`` never compares naive with aware.
    """
    if _is_naive(first) != _is_naive(second):
        raise ValueError(
            f"{fields} must both include a UTC offset or both omit it "
            "(times without an offset are read in the user's timezone)"
        )


class ListEventsInput(BaseModel):
    """Typed, bounded input of ``list_events`` (no tenant: it is implicit)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: datetime = Field(
        alias="from", description="Start of the range, ISO 8601 (offset optional)."
    )
    to: datetime = Field(description="End of the range, ISO 8601 (offset optional).")
    max_results: int = Field(default=MAX_RESULTS_DEFAULT, ge=1, le=MAX_RESULTS_LIMIT)
    calendar_id: str = Field(default="primary", min_length=1)

    @model_validator(mode="after")
    def _range_is_ordered(self) -> Self:
        _same_form(self.from_, self.to, "from and to")
        if self.from_ >= self.to:
            raise ValueError("from must be before to")
        return self


class CalendarEvent(BaseModel):
    """One event flattened for the model (Google ``items`` entry, trimmed)."""

    id: str
    summary: str | None = None
    start: str | None = None
    end: str | None = None
    attendees: list[str] = Field(default_factory=list)
    status: str | None = None


class ListEventsOutput(BaseModel):
    """The events of one range, validated before the model sees them."""

    events: list[CalendarEvent]


class CreateEventInput(BaseModel):
    """Typed, bounded input of ``create_event``."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_LENGTH)
    start: datetime
    end: datetime
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    attendees: list[str] = Field(default_factory=list, max_length=MAX_ATTENDEES)

    @field_validator("attendees")
    @classmethod
    def _attendees_are_present(cls, value: list[str]) -> list[str]:
        for email in value:
            if not email.strip():
                raise ValueError("attendee entries must be non-empty emails")
        return value

    @model_validator(mode="after")
    def _end_after_start(self) -> Self:
        _same_form(self.start, self.end, "start and end")
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class CreateEventOutput(BaseModel):
    """What the model needs to confirm a created event."""

    id: str
    html_link: str | None = None


class CancelEventInput(BaseModel):
    """Typed, bounded input of ``cancel_event``."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    calendar_id: str = Field(default="primary", min_length=1)


class CancelEventOutput(BaseModel):
    """Confirmation of one cancellation."""

    cancelled: bool
    event_id: str


async def _load_connection(
    session: AsyncSession, *, tenant_id: int, user_id: int
) -> UserConnection | None:
    """The user's newest ``calendar`` connection row (``None`` when absent).

    Same "newest wins" rule as the onboarding tester bridge: the MVP stores
    one provider row per module, and a reconnection reuses that row.
    """
    result = await session.execute(
        select(UserConnection)
        .where(
            UserConnection.tenant_id == tenant_id,
            UserConnection.user_id == user_id,
            UserConnection.module_key == MODULE_KEY,
        )
        .order_by(UserConnection.created_at.desc(), UserConnection.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _decrypt_blob(credentials_key: str | None, row: UserConnection) -> dict[str, Any] | None:
    """Decrypt the row's credentials JSON; ``None`` when absent or unreadable.

    A missing key, a malformed key, corrupted ciphertext, or a non-object
    blob all collapse to ``None`` — the caller maps that to the same
    "not configured" precheck and never echoes the underlying error.
    """
    if credentials_key is None or row.credentials_encrypted is None:
        return None
    try:
        blob: Any = json.loads(decrypt_credentials(credentials_key, row.credentials_encrypted))
    except (CredentialsKeyFormatError, DecryptionError, json.JSONDecodeError):
        return None
    return blob if isinstance(blob, dict) else None


def _is_expired(expires_at: Any) -> bool:
    """True when ``expires_at`` (epoch seconds) is missing-free and past skew.

    A blob without a parsable ``expires_at`` is treated as *not* expired:
    the provider's own 401 is the fallback (never guess a refresh).
    """
    if not isinstance(expires_at, (int, float)):
        return False
    return time.time() >= float(expires_at) - EXPIRY_SKEW_SECONDS


def _google_client_credentials(settings: Settings | None) -> tuple[str, str] | None:
    """The configured Google client id/secret, or ``None`` when unusable."""
    if settings is None:
        return None
    client_id = settings.google_client_id.get_secret_value() if settings.google_client_id else ""
    client_secret = (
        settings.google_client_secret.get_secret_value() if settings.google_client_secret else ""
    )
    if not client_id or not client_secret:
        return None
    return client_id, client_secret


def _calendar_catalog() -> ModuleCatalog:
    """A throwaway catalog holding only ``calendar`` for the refresh upsert.

    ``create_or_update`` validates its ``module_key`` against a catalog; the
    production default catalog deliberately lacks calendar until the T1
    allowlist change lands, so the precheck brings its own one-module view
    (the refresh must not depend on that pending decision).
    """
    catalog = ModuleCatalog()
    catalog.register(CALENDAR_MANIFEST)
    return catalog


async def _refresh_and_persist(
    context: ToolContext, *, row: UserConnection, refresh_token: str, blob: dict[str, Any]
) -> str:
    """Refresh the access token and store the new blob on the same connection.

    Raises:
        ToolPrecheckError: the refresh was refused or impossible (no client
            credentials configured) — the "reconnect" reason.
    """
    credentials = _google_client_credentials(context.settings)
    if credentials is None:
        raise ToolPrecheckError(EXPIRED)
    client_id, client_secret = credentials
    try:
        tokens = await oauth_service.refresh_access_token(
            refresh_token=refresh_token, client_id=client_id, client_secret=client_secret
        )
    except (oauth_service.TokenExchangeError, httpx.RequestError):
        logger.warning("calendar token refresh failed; the user must reconnect", exc_info=True)
        raise ToolPrecheckError(EXPIRED) from None
    refreshed: dict[str, Any] = {
        "access_token": tokens.access_token,
        # Google may omit the refresh token on refresh: keep the stored one.
        "refresh_token": tokens.refresh_token or refresh_token,
        "scopes": list(tokens.scopes) or list(blob.get("scopes") or []),
    }
    if tokens.expires_in is not None:
        refreshed["expires_at"] = int(time.time()) + tokens.expires_in
    new_blob = json.dumps(refreshed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    await create_or_update(
        context.session,
        catalog=_calendar_catalog(),
        credentials_key=context.credentials_key,
        caller=context.user,
        module_key=MODULE_KEY,
        provider=row.provider,
        credentials=new_blob,
    )
    return tokens.access_token


async def _live_connection(context: ToolContext) -> tuple[UserConnection, str]:
    """The user's calendar connection plus a usable access token.

    Raises:
        ToolPrecheckError: no connection, no blob, an unusable ``credentials_key``,
            an unreadable blob (all ``NOT_CONFIGURED``), or an expired token
            that could not be refreshed (``EXPIRED``). Never raises anything
            else for connection problems — the executor masks those as 500s.
    """
    row = await _load_connection(
        context.session, tenant_id=context.tenant_id, user_id=context.user.id
    )
    if row is None:
        raise ToolPrecheckError(NOT_CONFIGURED)
    blob = _decrypt_blob(context.credentials_key, row)
    if blob is None:
        raise ToolPrecheckError(NOT_CONFIGURED)
    access_token = blob.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ToolPrecheckError(NOT_CONFIGURED)
    if not _is_expired(blob.get("expires_at")):
        return row, access_token
    refresh_token = blob.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ToolPrecheckError(EXPIRED)
    new_token = await _refresh_and_persist(context, row=row, refresh_token=refresh_token, blob=blob)
    return row, new_token


def _provider_for(row: UserConnection, access_token: str) -> CalendarProvider:
    """Build the per-user provider from the row's registered factory."""
    factory = get_provider(row.provider)
    return factory(access_token=access_token)


async def calendar_precheck(context: ToolContext, validated_input: BaseModel) -> None:
    """Verify the connection is live before any handler runs (shared by all three).

    Loads and decrypts the stored credentials, refreshing an expired access
    token when possible. It never builds a provider — a refused call can
    never reach the external API.
    """
    await _live_connection(context)


def _user_zone(name: str | None) -> ZoneInfo | None:
    """The requesting user's tz database zone, or ``None`` when absent or unusable.

    The agent run already resolved ``ToolContext.timezone`` (``"UTC"`` on
    fallback); it is ``None`` outside chat runs. An unusable name is only a
    defensive path here and never fails the call.
    """
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _localize(value: datetime, zone: ZoneInfo | None) -> datetime:
    """Read a naive ``value`` as wall-clock time in ``zone`` (UTC when ``None``).

    Offset-bearing values are returned untouched. DST edges use ``fold=0``
    on purpose, so they are deterministic and never an error: a repeated
    time (clocks going back) is its first occurrence, and a skipped time
    (clocks going forward) keeps the offset in effect before the change.
    """
    if not _is_naive(value):
        return value
    return value.replace(tzinfo=zone or UTC, fold=0)


def _time_zone_argument(zone: ZoneInfo | None) -> dict[str, str]:
    """The provider's ``time_zone`` keyword, omitted when there is no usable zone."""
    return {"time_zone": zone.key} if zone is not None else {}


def _map_event(item: dict[str, Any]) -> CalendarEvent:
    """Flatten one Google ``items`` entry into a :class:`CalendarEvent`."""
    start = item.get("start") if isinstance(item.get("start"), dict) else {}
    end = item.get("end") if isinstance(item.get("end"), dict) else {}
    raw_attendees = item.get("attendees")
    attendees = (
        [
            str(attendee.get("email"))
            for attendee in raw_attendees
            if isinstance(attendee, dict) and attendee.get("email")
        ]
        if isinstance(raw_attendees, list)
        else []
    )
    return CalendarEvent(
        id=str(item.get("id", "")),
        summary=item.get("summary") if isinstance(item.get("summary"), str) else None,
        start=start.get("dateTime") or start.get("date"),
        end=end.get("dateTime") or end.get("date"),
        attendees=attendees,
        status=item.get("status") if isinstance(item.get("status"), str) else None,
    )


async def list_events_handler(
    context: ToolContext, validated_input: ListEventsInput
) -> ListEventsOutput:
    """Return the events of one calendar inside the requested range."""
    row, access_token = await _live_connection(context)
    provider = _provider_for(row, access_token)
    zone = _user_zone(context.timezone)
    items = await provider.list_events(
        calendar_id=validated_input.calendar_id,
        max_results=validated_input.max_results,
        time_min=_localize(validated_input.from_, zone).isoformat(),
        time_max=_localize(validated_input.to, zone).isoformat(),
        **_time_zone_argument(zone),
    )
    return ListEventsOutput(events=[_map_event(item) for item in items if isinstance(item, dict)])


async def create_event_handler(
    context: ToolContext, validated_input: CreateEventInput
) -> CreateEventOutput:
    """Insert one event on the user's primary calendar."""
    row, access_token = await _live_connection(context)
    provider = _provider_for(row, access_token)
    zone = _user_zone(context.timezone)
    payload = await provider.create_event(
        summary=validated_input.summary,
        start=_localize(validated_input.start, zone).isoformat(),
        end=_localize(validated_input.end, zone).isoformat(),
        description=validated_input.description,
        attendees=validated_input.attendees or None,
        **_time_zone_argument(zone),
    )
    return CreateEventOutput.model_validate(payload)


async def cancel_event_handler(
    context: ToolContext, validated_input: CancelEventInput
) -> CancelEventOutput:
    """Delete one event by id (the user's own calendar, D5 self-service)."""
    row, access_token = await _live_connection(context)
    provider = _provider_for(row, access_token)
    await provider.delete_event(
        calendar_id=validated_input.calendar_id, event_id=validated_input.event_id
    )
    return CancelEventOutput(cancelled=True, event_id=validated_input.event_id)


LIST_EVENTS = ToolSpec(
    name="list_events",
    description=(
        "List the user's calendar events inside a time range, each with id, "
        f"summary, start, end, attendees, and status. {TIMES_NOTE}"
    ),
    kind=ToolKind.EXTRACT,
    approval_policy=ApprovalPolicy.ALLOW_WITHOUT_APPROVAL,
    required_role=UserRole.VIEWER,
    handler=list_events_handler,
    input_model=ListEventsInput,
    output_model=ListEventsOutput,
    precheck=calendar_precheck,
    module=MODULE_KEY,
)

CREATE_EVENT = ToolSpec(
    name="create_event",
    description=(
        "Create one event on the user's primary calendar (summary, start, "
        f"end; optional description and attendee emails). {TIMES_NOTE}"
    ),
    kind=ToolKind.ACTION,
    approval_policy=ApprovalPolicy.ALLOW_WITHOUT_APPROVAL,
    required_role=UserRole.OPERATOR,
    handler=create_event_handler,
    input_model=CreateEventInput,
    output_model=CreateEventOutput,
    precheck=calendar_precheck,
    module=MODULE_KEY,
)

CANCEL_EVENT = ToolSpec(
    name="cancel_event",
    description="Cancel (delete) one of the user's calendar events by id.",
    kind=ToolKind.ACTION,
    approval_policy=ApprovalPolicy.ALLOW_WITHOUT_APPROVAL,
    required_role=UserRole.OPERATOR,
    handler=cancel_event_handler,
    input_model=CancelEventInput,
    output_model=CancelEventOutput,
    precheck=calendar_precheck,
    module=MODULE_KEY,
)


def register_calendar_tools(registry: ToolRegistry) -> None:
    """Register the three calendar tools on ``registry``.

    Explicit entry point: importing this module registers nothing.
    ``app/main.py`` calls it once at import time on ``default_registry``
    (the human-approved allowlist entry that
    ``tests/test_tools_executor.py``'s allowlist test locks); tests call it
    against a LOCAL registry.
    """
    for spec in (LIST_EVENTS, CREATE_EVENT, CANCEL_EVENT):
        registry.register(spec)


__all__ = [
    "CANCEL_EVENT",
    "CREATE_EVENT",
    "LIST_EVENTS",
    "MODULE_KEY",
    "CalendarEvent",
    "CancelEventInput",
    "CancelEventOutput",
    "CreateEventInput",
    "CreateEventOutput",
    "ListEventsInput",
    "ListEventsOutput",
    "register_calendar_tools",
]
