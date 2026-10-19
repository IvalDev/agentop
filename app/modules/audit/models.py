"""Append-only audit log: one immutable row per audited action.

Rows are written only through :mod:`app.modules.audit.service`; nothing in
the application updates or deletes them. Three ORM guards enforce that:

- ``before_update`` / ``before_delete`` mapper events refuse unit-of-work
  changes to a loaded :class:`AuditEvent` (``AuditImmutableError`` at flush);
- a ``do_orm_execute`` session event refuses ``UPDATE``/``DELETE``
  statements on ``audit_events`` sent through any ORM session, ORM-enabled
  (``update(AuditEvent)``) or Core (``update(AuditEvent.__table__)``).

Raw SQL on a bare connection bypasses the ORM; the database-level guard (a
PostgreSQL trigger rejecting ``UPDATE``/``DELETE`` plus revoked privileges)
belongs to the T12 migration baseline.

The table deliberately has no foreign keys: the trail must outlive users and
tenants it mentions, and failure events are written on their own connection
(see the service) where locks held by the business transaction must not
matter. ``tenant_id`` is still mandatory and every read filters on it.
"""

import enum
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Enum, Index, Integer, String, Uuid, event
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column

from app.core.db import Base
from app.modules.approvals.models import UTCDateTime


class AuditImmutableError(RuntimeError):
    """Raised on any attempt to update or delete an audit event."""


class ActorType(enum.StrEnum):
    """Who performed an audited action."""

    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class AuditEvent(Base):
    """One audited action: who did what to which resource, with what result.

    ``action`` is a dotted lowercase verb (``tool.executed``,
    ``approval.expired``, ``document.ingest_failed`` …). ``payload`` is the
    sanitized, size-capped input of the action and ``result`` its outcome.
    ``correlation_id`` threads related events (an approval id across its
    lifecycle and the tool run it authorizes, or a caller-supplied chat id).
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_events_tenant_action", "tenant_id", "action"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[int] = mapped_column(Integer)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_type: Mapped[ActorType] = mapped_column(Enum(ActorType, name="audit_actor_type"))
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[Any | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    result: Mapped[Any | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


@event.listens_for(AuditEvent, "before_update")
def _refuse_update(mapper: Any, connection: Any, target: AuditEvent) -> None:
    raise AuditImmutableError("audit events are append-only: update refused")


@event.listens_for(AuditEvent, "before_delete")
def _refuse_delete(mapper: Any, connection: Any, target: AuditEvent) -> None:
    raise AuditImmutableError("audit events are append-only: delete refused")


@event.listens_for(Session, "do_orm_execute")
def _refuse_bulk_mutation(state: ORMExecuteState) -> None:
    """Refuse ``UPDATE``/``DELETE`` statements on ``audit_events`` in any session."""
    if not (state.is_update or state.is_delete):
        return
    table = getattr(state.statement, "table", None)
    if getattr(table, "name", None) == AuditEvent.__tablename__:
        raise AuditImmutableError("audit events are append-only: bulk mutation refused")
