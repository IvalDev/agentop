"""Idempotent bootstrap of the default tenant and its owner user.

Used by the ``make bootstrap`` target and by tests. Running it any number of
times against the same database leaves exactly one tenant (``default`` by
slug) and one owner per bootstrap email; existing rows are never mutated, so
a forgotten password is not silently reset by re-running the script.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import Database, session_scope
from app.modules.identity.models import Tenant, User, UserRole
from app.modules.identity.security import hash_password

logger = logging.getLogger(__name__)


async def ensure_bootstrap_tenant_and_owner(
    db: AsyncSession,
    email: str,
    password: str,
    tenant_slug: str = "default",
) -> User:
    """Create the tenant and owner user when missing; return the owner.

    The caller owns the transaction (e.g. via ``session_scope``); this
    function only adds missing rows and flushes them.
    """
    tenant_result = await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
    tenant = tenant_result.scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(name=tenant_slug, slug=tenant_slug, is_active=True)
        db.add(tenant)
        await db.flush()

    user_result = await db.execute(
        select(User).where(User.tenant_id == tenant.id, User.email == email)
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        user = User(
            tenant_id=tenant.id,
            email=email,
            full_name="Bootstrap Owner",
            hashed_password=hash_password(password),
            role=UserRole.OWNER,
            is_active=True,
        )
        db.add(user)
        await db.flush()
    return user


async def _run(settings: Settings) -> None:
    if not settings.database_url:
        raise SystemExit("AA_DATABASE_URL must be set for bootstrap")
    db = Database(settings.database_url)
    try:
        async with session_scope(db) as session:
            user = await ensure_bootstrap_tenant_and_owner(
                session, settings.bootstrap_email, settings.bootstrap_password
            )
        logger.info("bootstrap ensured", extra={"user_id": user.id, "tenant_id": user.tenant_id})
    finally:
        await db.dispose()


def main() -> None:
    """CLI entry point: ``python -m app.modules.identity.seed``."""
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    if not settings.bootstrap_email or not settings.bootstrap_password:
        raise SystemExit("AA_BOOTSTRAP_EMAIL and AA_BOOTSTRAP_PASSWORD must be set")
    asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
