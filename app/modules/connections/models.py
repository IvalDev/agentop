"""Connection models: one user's provider connections with encrypted credentials.

Tenant + titularity isolation (decisions A8/D5)
------------------------------------------------
Same column-based tenant rule as the identity models: tenants share single
tables, every row carries ``tenant_id``, and every query MUST filter by it.
On top of that, a connection belongs to exactly one *user* (its ``user_id``,
decision D5): owner-scoped queries filter by both ``tenant_id`` and
``user_id``; only the admin routes (tenant listing, revoke) look across
users, still inside the caller's tenant.

``UNIQUE (tenant_id, user_id, module_key, provider)`` gives one connection per
user per module provider, so create-or-update flips the same row instead of
duplicating.

Secrets versus display data
---------------------------
``credentials_encrypted`` holds a Fernet token (see :mod:`app.core.crypto`);
the encryption key lives in ``Settings.credentials_key`` — outside the
database, passed per request. The ciphertext never appears in an API
response. ``metadata_`` is the *display* counterpart: non-secret labels and
endpoints (account name, calendar URL …), safe for the admin listing.

The ORM attribute is ``metadata_`` (trailing underscore) because ``metadata``
is reserved by SQLAlchemy's declarative system; the database column is still
named ``metadata``.

``status`` stores the :class:`ConnectionStatus` name inside an ``Enum``
column — same pattern as ``User.role`` (``user_role``): queries compare enum
members, never raw strings.
"""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class ConnectionStatus(enum.StrEnum):
    """Lifecycle of one user's connection to one provider.

    ``pending`` (created without credentials) → ``connected`` (credentials
    stored) → ``expired``/``failed`` (health checks, T4) or ``revoked``
    (the stored secret was destroyed).
    """

    PENDING = "pending"
    CONNECTED = "connected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    FAILED = "failed"


class UserConnection(TimestampMixin, Base):
    """One user's connection to one provider of one catalog module."""

    __tablename__ = "user_connections"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "module_key",
            "provider",
            name="uq_user_connections_tenant_user_module_provider",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    #: The connection's owner (decision D5): everyone configures their own.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    #: Key of a module declared in the catalog (``ModuleManifest.key``).
    module_key: Mapped[str] = mapped_column(String(64))
    #: Lowercase provider identifier within the module (``caldav`` / ``google``).
    provider: Mapped[str] = mapped_column(String(64))
    status: Mapped[ConnectionStatus] = mapped_column(
        Enum(ConnectionStatus, name="connection_status"),
        default=ConnectionStatus.PENDING,
    )
    #: Fernet token of the provider credentials — never returned by any API.
    credentials_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: NON-secret display data only (account label, scopes, endpoint …).
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    #: Last successful health check (T4 writes it); null until one runs.
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
