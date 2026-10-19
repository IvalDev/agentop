"""Identity service functions: credentials, refresh sessions, scoped queries."""

import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.deps import TenantContext
from app.modules.identity.models import RefreshSession, Tenant, User
from app.modules.identity.security import (
    generate_refresh_token,
    hash_refresh_token,
    verify_password,
)

logger = logging.getLogger(__name__)

#: Randomness of a session family id (``token_hex`` → 32 characters).
FAMILY_ID_BYTES = 16


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


def _as_utc(value: datetime) -> datetime:
    """Re-attach UTC to timestamps SQLite returns naive (Postgres keeps them aware)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _load_refresh_session(db: AsyncSession, raw_token: str) -> RefreshSession | None:
    """Look up a session by the digest of the presented raw token."""
    result = await db.execute(
        select(RefreshSession).where(RefreshSession.token_hash == hash_refresh_token(raw_token))
    )
    return result.scalar_one_or_none()


async def create_refresh_session(
    db: AsyncSession, user: User, *, expires_days: int, family_id: str | None = None
) -> tuple[RefreshSession, str]:
    """Create a refresh session and return it with its raw cookie token.

    The raw token is returned exactly once (it goes into the httpOnly cookie);
    only its digest is persisted. A new family starts per login; rotations
    pass the original ``family_id``.
    """
    raw_token = generate_refresh_token()
    session = RefreshSession(
        user_id=user.id,
        token_hash=hash_refresh_token(raw_token),
        family_id=family_id or secrets.token_hex(FAMILY_ID_BYTES),
        expires_at=datetime.now(tz=UTC) + timedelta(days=expires_days),
    )
    db.add(session)
    await db.flush()
    return session, raw_token


async def revoke_refresh_family(db: AsyncSession, family_id: str) -> int:
    """Revoke every still-active session of a family; return how many were hit."""
    result = await db.execute(
        update(RefreshSession)
        .where(RefreshSession.family_id == family_id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(tz=UTC))
    )
    return int(result.rowcount or 0)


async def revoke_refresh_session(db: AsyncSession, raw_token: str) -> bool:
    """Revoke exactly the presented session (per-device logout)."""
    session = await _load_refresh_session(db, raw_token)
    if session is None or session.revoked_at is not None:
        return False
    session.revoked_at = datetime.now(tz=UTC)
    await db.flush()
    return True


async def rotate_refresh_session(
    db: AsyncSession, raw_token: str, *, expires_days: int
) -> tuple[User, str] | None:
    """Validate and rotate a refresh cookie; return the user plus a new token.

    Every rejection (unknown, revoked, expired, missing/deactivated user)
    returns ``None`` so callers answer one uniform 401. Replaying a revoked
    token is treated as theft: the whole family is revoked first, so the
    newest token dies with it.
    """
    session = await _load_refresh_session(db, raw_token)
    if session is None:
        return None
    if session.revoked_at is not None:
        revoked = await revoke_refresh_family(db, session.family_id)
        logger.warning(
            "refresh token reuse detected; revoked session family",
            extra={"user_id": session.user_id, "family_id": session.family_id, "revoked": revoked},
        )
        return None
    if _as_utc(session.expires_at) <= datetime.now(tz=UTC):
        return None
    user = await db.get(User, session.user_id)
    if user is None or not user.is_active:
        return None
    replacement, new_raw_token = await create_refresh_session(
        db, user, expires_days=expires_days, family_id=session.family_id
    )
    session.revoked_at = datetime.now(tz=UTC)
    session.replaced_by_id = replacement.id
    await db.flush()
    return user, new_raw_token


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
