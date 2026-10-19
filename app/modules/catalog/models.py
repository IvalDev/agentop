"""Catalog models: per-tenant module activation.

Tenant isolation (same column-based rule as architecture decision A8)
--------------------------------------------------------------------
Tenants share single tables and every activation row carries a
``tenant_id`` column. Every query MUST filter by ``tenant_id``
(:func:`app.modules.catalog.service.active_module_keys` does); rows of other
tenants are never visible, and ``UNIQUE (tenant_id, module_key)`` guarantees
one activation state per module per tenant, so a repeated activate can never
duplicate rows.

Activation is a tenant-level switch: an admin turns the module on for
everyone in the tenant, and per-user connection state lives elsewhere (T3's
``UserConnection``). ``activated_by_user_id`` records who last turned the
module on and intentionally survives deactivation — who removed it is in the
audit trail (``module.deactivated``), not in the row.
"""

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class ModuleActivation(TimestampMixin, Base):
    """One tenant's on/off state for one catalog module."""

    __tablename__ = "module_activations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "module_key", name="uq_module_activations_tenant_module"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    module_key: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    activated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
