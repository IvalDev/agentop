"""Identity models: tenants, users, and roles.

Tenant isolation (simplification of architecture decision A8)
------------------------------------------------------------
The long-term plan is one PostgreSQL schema per tenant in the same cluster.
For this milestone tenants share single tables and every row that belongs to
a tenant carries a ``tenant_id`` column. All queries MUST filter by
``tenant_id`` (use :func:`app.modules.identity.service.scoped_query`); the
per-tenant schema move is a later migration and must not change the public
API.

Permission matrix (RBAC)
------------------------
- ``owner``: full control of the tenant, including tenant deletion.
- ``admin``: manage users and configuration; cannot delete the tenant.
- ``operator``: run agent tools and approve medium-sensitivity actions.
- ``viewer``: read-only access plus chat queries.

``User.is_superadmin`` is reserved for cross-tenant operators of a future
control plane; in this milestone it only bypasses in-tenant role checks.
"""

import enum

from sqlalchemy import Boolean, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class UserRole(enum.StrEnum):
    """RBAC roles ordered from most to least privileged."""

    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class Tenant(TimestampMixin, Base):
    """A tenant (SMB customer) that owns users and all tenant-scoped data."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class User(TimestampMixin, Base):
    """A user belonging to exactly one tenant, identified by email per tenant."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    full_name: Mapped[str] = mapped_column(String(200), default="")
    hashed_password: Mapped[str] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), default=UserRole.VIEWER
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)
