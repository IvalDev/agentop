"""Tenant isolation (scoped queries) and bootstrap seed idempotency tests."""

from sqlalchemy import select

from app.core.db import Database, session_scope
from app.modules.identity.deps import TenantContext
from app.modules.identity.models import Tenant, User, UserRole
from app.modules.identity.seed import ensure_bootstrap_tenant_and_owner
from app.modules.identity.service import scoped_query


async def test_scoped_query_excludes_other_tenants(seeded_db: Database) -> None:
    """A tenant-scoped select never returns rows belonging to another tenant."""
    async with seeded_db.sessionmaker() as session:
        acme_owner = (
            await session.execute(select(User).where(User.email == "owner@acme.test"))
        ).scalar_one()
        globex_owner = (
            await session.execute(select(User).where(User.email == "owner@globex.test"))
        ).scalar_one()
        acme_context = TenantContext(
            tenant_id=acme_owner.tenant_id, user_id=acme_owner.id, role=UserRole.OWNER
        )
        globex_context = TenantContext(
            tenant_id=globex_owner.tenant_id,
            user_id=globex_owner.id,
            role=UserRole.OWNER,
        )

        acme_rows = (await session.execute(scoped_query(User, acme_context))).scalars().all()
        globex_rows = (await session.execute(scoped_query(User, globex_context))).scalars().all()

    assert {u.email for u in acme_rows} == {
        "owner@acme.test",
        "admin@acme.test",
        "viewer@acme.test",
    }
    assert {u.email for u in globex_rows} == {"owner@globex.test"}


async def test_bootstrap_seed_is_idempotent(identity_db: Database) -> None:
    """Running the bootstrap twice creates exactly one tenant and one owner."""
    async with session_scope(identity_db) as session:
        first = await ensure_bootstrap_tenant_and_owner(session, "root@example.com", "boot-pass")
    async with session_scope(identity_db) as session:
        second = await ensure_bootstrap_tenant_and_owner(session, "root@example.com", "boot-pass")
        users = (await session.execute(select(User))).scalars().all()
        tenants = (await session.execute(select(Tenant))).scalars().all()

    assert first.id == second.id
    assert second.role == UserRole.OWNER
    assert len(users) == 1
    assert len(tenants) == 1
    assert tenants[0].slug == "default"


async def test_bootstrap_creates_logins_usable_by_auth(identity_db: Database) -> None:
    """The seeded owner can authenticate with the bootstrap credentials."""
    from app.modules.identity.service import authenticate

    async with session_scope(identity_db) as session:
        await ensure_bootstrap_tenant_and_owner(session, "root@example.com", "boot-pass")
    async with identity_db.sessionmaker() as session:
        user = await authenticate(session, "root@example.com", "boot-pass", "default")
    assert user is not None
    assert user.role == UserRole.OWNER
    assert user.email == "root@example.com"
