"""Connector models: paired devices and their single-use pairing codes.

A ``Connector`` is one user's local agent on one device. Like
:class:`~app.modules.identity.models.RefreshSession`, only a SHA-256 digest of
the bearer token is stored: the raw token is returned once at pairing time and
never persisted. ``ConnectorPairingCode`` follows the same rule for the short
code the user types into the widget; the row is single-use (``used_at``) and
expires after ten minutes.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class Connector(TimestampMixin, Base):
    """One paired device that serves the user's granted local folder."""

    __tablename__ = "connectors"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConnectorPairingCode(TimestampMixin, Base):
    """Short-lived, single-use code that pairs one local connector.

    The client shows the formatted code ``XXXX-XXXX``; the row keeps only
    ``sha256`` of its normalized form (uppercase, no hyphens).
    """

    __tablename__ = "connector_pairing_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
