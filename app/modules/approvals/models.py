"""Approval request model: one human decision bound to one exact tool call.

Status machine (every arrow is a conditional ``UPDATE ... WHERE status = ?``
so concurrent callers cannot both win a transition)::

    pending ──approve──▶ approved ──claim──▶ executing ──▶ executed | failed
       │                    │
       ├──reject──▶ rejected│
       └──TTL──▶ expired ◀──┘ (TTL)

``rejected``, ``expired``, ``executed``, and ``failed`` are terminal;
``executing`` is the durable single-use claim taken before the tool runs, so
a crash mid-execution never re-opens the request. Rows are tenant-scoped
(decision A8 simplification): every query MUST filter on ``tenant_id``.

The ``*_by``/``*_at`` columns record the latest decision and execution; the
full history lives in the audit log (T8), written by the approvals service.
"""

import enum
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.core.db import Base


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend.

    PostgreSQL keeps ``timestamptz``; SQLite drops ``tzinfo`` on the way in
    and returns naive values. Binding converts to UTC and loading re-attaches
    UTC, so TTL comparisons work identically in Python and in SQL.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime requires a timezone-aware datetime")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class ApprovalStatus(enum.StrEnum):
    """Lifecycle of an approval request (see the module docstring)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"


class ApprovalRequest(Base):
    """A request to run ``tool_name`` with exactly ``input_payload``.

    ``action_key`` is computed server-side from the tool name and the
    canonical validated input (``compute_action_key``) when the request is
    created; execution re-derives it from the stored payload and refuses on
    any mismatch. ``created_at`` comes from the service's injected clock
    (not a server default) because ``expires_at`` is derived from it.
    """

    __tablename__ = "approval_requests"
    __table_args__ = (Index("ix_approval_requests_tenant_status", "tenant_id", "status"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    tool_name: Mapped[str] = mapped_column(String(64))
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    action_key: Mapped[str] = mapped_column(String(160))
    required_role: Mapped[str] = mapped_column(String(32))
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, name="approval_status"), default=ApprovalStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    execution_result: Mapped[Any | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    execution_error: Mapped[str | None] = mapped_column(Text, nullable=True)
