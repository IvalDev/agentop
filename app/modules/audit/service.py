"""Audit service: record sanitized events and query one tenant's trail.

Write paths — pick by what the event describes:

:func:`stage_event`
    Adds the event to the caller's session, so it commits or rolls back
    together with the database change it describes (``document.uploaded``,
    ``approval.approved``, ``approval.expired`` …): the event exists iff the
    change exists. Use it only where that unit of work is committed — by the
    caller, or by the service itself on its failure paths.

:func:`persist_event`
    Writes the event in its own short transaction on a new session bound to
    the caller's engine and commits it immediately, so it survives any later
    rollback or error of the caller. Used for refusals, failures, and every
    tool-execution outcome (a handler's external effects — an email sent —
    are not undone by a database rollback, so the trail must not be either).

On PostgreSQL the second session has its own pooled connection, so the
audit commit never commits or waits on the caller's uncommitted work. The
in-memory SQLite test database shares one connection (``StaticPool``), so
there the audit commit also commits anything the caller already *flushed*;
true isolation is a PostgreSQL property (T12 integration follow-up).

Hygiene, applied to ``payload`` and ``result`` before any write:

- values are converted to plain JSON (``pydantic_core.to_jsonable_python``);
- secret-looking keys (password, token, secret, api key, grant, …) are
  replaced by :data:`REDACTED` at any depth, and bearer/JWT strings inside
  text are masked;
- a value whose compact JSON exceeds ``Settings.audit_max_payload_bytes``
  (see :func:`configure_audit`) is replaced by
  ``{"truncated": true, "original_bytes": n, "preview": "<first bytes>"}``.

Callers decide *what* to store: canonical validated input, never raw
unvalidated input (field names and error kinds only) and never grants.
"""

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Required, TypedDict, Unpack
from uuid import UUID, uuid4

from pydantic_core import to_jsonable_python
from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.modules.audit.models import ActorType, AuditEvent
from app.modules.identity.deps import TenantContext
from app.modules.identity.service import scoped_query

REDACTED = "[redacted]"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

#: Normalized key fragments (lowercase, alphanumerics only) that mark a secret.
_SECRET_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "authorization",
    "credential",
    "grant",
    "cookie",
    "privatekey",
)
_BEARER = re.compile(r"(?i)\b(bearer)\s+[\w.~+/=-]+")
_JWT = re.compile(r"\beyJ[\w-]*\.[\w-]+\.[\w-]+")
_ACTION = re.compile(r"[a-z][a-z_]*(\.[a-z][a-z_]*)+")

_max_payload_bytes = Settings.model_fields["audit_max_payload_bytes"].default


class InvalidCursorError(ValueError):
    """Raised when a pagination cursor is malformed or was tampered with."""


class EventFields(TypedDict, total=False):
    """Keyword arguments of :func:`stage_event` and :func:`persist_event`.

    ``actor_type`` defaults to ``user`` when ``actor_user_id`` is given and
    to ``system`` otherwise; ``now`` (timezone-aware) defaults to UTC now.
    """

    tenant_id: Required[int]
    action: Required[str]
    actor_user_id: int | None
    actor_type: ActorType | None
    resource_type: str | None
    resource_id: str | None
    tool_name: str | None
    payload: Any
    result: Any
    correlation_id: str | None
    now: datetime | None


@dataclass(frozen=True)
class AuditFilters:
    """Optional filters of :func:`list_events` (``created_to`` is exclusive)."""

    actor_user_id: int | None = None
    action: str | None = None
    action_prefix: str | None = None
    tool_name: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    correlation_id: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None


def configure_audit(settings: Settings) -> None:
    """Apply ``settings.audit_max_payload_bytes`` (called by ``create_app``)."""
    global _max_payload_bytes
    _max_payload_bytes = settings.audit_max_payload_bytes


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(marker in normalized for marker in _SECRET_KEY_MARKERS)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): REDACTED if _is_secret_key(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _JWT.sub(REDACTED, _BEARER.sub(rf"\1 {REDACTED}", value))
    return value


def _cap(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) <= _max_payload_bytes:
        return value
    preview = encoded[:_max_payload_bytes].decode(errors="ignore")
    return {"truncated": True, "original_bytes": len(encoded), "preview": preview}


def _clean(value: Any) -> Any:
    """Plain-JSON, redacted, size-capped copy of ``value`` (``None`` stays ``None``)."""
    if value is None:
        return None
    return _cap(_redact(to_jsonable_python(value, fallback=str)))


def _clip(value: str | None, length: int) -> str | None:
    return None if value is None else value[:length]


def _build_event(
    *,
    tenant_id: int,
    action: str,
    actor_user_id: int | None = None,
    actor_type: ActorType | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    tool_name: str | None = None,
    payload: Any = None,
    result: Any = None,
    correlation_id: str | None = None,
    now: datetime | None = None,
) -> AuditEvent:
    if len(action) > 64 or not _ACTION.fullmatch(action):
        raise ValueError(f"audit action must be a dotted lowercase verb, got {action!r}")
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if actor_type is None:
        actor_type = ActorType.SYSTEM if actor_user_id is None else ActorType.USER
    return AuditEvent(
        id=uuid4(),
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        action=action,
        resource_type=_clip(resource_type, 32),
        resource_id=_clip(resource_id, 64),
        tool_name=_clip(tool_name, 64),
        payload=_clean(payload),
        result=_clean(result),
        correlation_id=_clip(correlation_id, 64),
        created_at=now.astimezone(UTC),
    )


def stage_event(session: AsyncSession, **fields: Unpack[EventFields]) -> AuditEvent:
    """Add an event to ``session``; it commits or rolls back with the caller."""
    event = _build_event(**fields)
    session.add(event)
    return event


async def persist_event(session: AsyncSession, **fields: Unpack[EventFields]) -> AuditEvent:
    """Commit an event in its own transaction, independent of ``session``'s.

    ``session`` only supplies the engine (``session.bind``); its own
    transaction is neither flushed, committed, nor rolled back. A failure to
    write propagates: an action whose refusal cannot be audited fails loudly.

    SQLite single-writer guard: the caller session must NOT hold an
    uncommitted write (a flushed row) when this runs — the independent audit
    connection would deadlock and raise ``database is locked`` on a
    file-backed database (invisible under the shared ``:memory:`` test
    connection). When a state change and its audit event belong together,
    use :func:`stage_event` on the caller's transaction instead.
    """
    event = _build_event(**fields)
    bind = session.bind
    if bind is None:
        raise RuntimeError("persist_event needs a session bound to an engine")
    engine = bind if isinstance(bind, AsyncEngine) else bind.engine
    async with AsyncSession(bind=engine, expire_on_commit=False) as audit_session:
        audit_session.add(event)
        await audit_session.commit()
    return event


def _encode_cursor(event: AuditEvent) -> str:
    raw = json.dumps({"t": event.created_at.isoformat(), "id": str(event.id)})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        created_at = datetime.fromisoformat(data["t"])
        event_id = UUID(data["id"])
    except (binascii.Error, ValueError, TypeError, KeyError) as exc:
        raise InvalidCursorError("invalid cursor") from exc
    if created_at.tzinfo is None:
        raise InvalidCursorError("invalid cursor")
    return created_at, event_id


async def list_events(
    session: AsyncSession,
    context: TenantContext,
    filters: AuditFilters,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> tuple[list[AuditEvent], str | None]:
    """Return one page of the tenant's events, newest first, and the next cursor.

    Keyset pagination over ``(created_at, id)`` descending: pages stay stable
    while new events are appended. ``next_cursor`` is ``None`` on the last
    page. Raises :class:`InvalidCursorError` for a malformed cursor.
    """
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    query = scoped_query(AuditEvent, context)
    exact = {
        AuditEvent.actor_user_id: filters.actor_user_id,
        AuditEvent.action: filters.action,
        AuditEvent.tool_name: filters.tool_name,
        AuditEvent.resource_type: filters.resource_type,
        AuditEvent.resource_id: filters.resource_id,
        AuditEvent.correlation_id: filters.correlation_id,
    }
    for column, value in exact.items():
        if value is not None:
            query = query.where(column == value)
    if filters.action_prefix is not None:
        query = query.where(AuditEvent.action.startswith(filters.action_prefix, autoescape=True))
    if filters.created_from is not None:
        query = query.where(AuditEvent.created_at >= filters.created_from)
    if filters.created_to is not None:
        query = query.where(AuditEvent.created_at < filters.created_to)
    if cursor is not None:
        created_at, event_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                AuditEvent.created_at < created_at,
                and_(AuditEvent.created_at == created_at, AuditEvent.id < event_id),
            )
        )
    query = query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit + 1)
    rows = list((await session.execute(query)).scalars())
    next_cursor = _encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor
