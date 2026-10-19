"""Identity service functions: credential checks and tenant-scoped queries."""

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.deps import TenantContext
from app.modules.identity.models import Tenant, User
from app.modules.identity.security import verify_password


async def authenticate(
    db: AsyncSession, email: str, password: str, tenant_slug: str
) -> User | None:
    """Return the active user when credentials are valid, else ``None``.

    Every failure mode (unknown tenant, inactive tenant, unknown email,
    inactive user, wrong password) returns ``None`` so callers produce one
    uniform "invalid credentials" error and cannot enumerate users or
    tenants. Passwords are never logged or included in the result.
    """
    tenant_result = await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
    tenant = tenant_result.scalar_one_or_none()
    if tenant is None or not tenant.is_active:
        return None
    user_result = await db.execute(
        select(User).where(User.tenant_id == tenant.id, User.email == email)
    )
    user = user_result.scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


def scoped_query[T](model: type[T], tenant_context: TenantContext) -> Select[tuple[T]]:
    """Return a ``SELECT`` restricted to the context's tenant.

    Tenant isolation for this milestone is column-based (decision A8
    simplification): every tenant-owned model exposes ``tenant_id``, and all
    reads must pass through this helper so rows from other tenants are never
    visible. The future per-tenant schema migration keeps this signature.
    """
    if not hasattr(model, "tenant_id"):
        raise TypeError(f"{model.__name__} has no tenant_id column; cannot scope")
    tenant_column = model.tenant_id
    return select(model).where(tenant_column == tenant_context.tenant_id)
