"""FastAPI dependencies: authentication, role guards, and tenant context."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import get_session
from app.core.deps import get_settings
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import TokenClaims, TokenError, decode_token

bearer_scheme = HTTPBearer(auto_error=False)

_AUTH_HEADERS = {"WWW-Authenticate": "Bearer"}


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated user loaded from the database for this request."""

    id: int
    tenant_id: int
    email: str
    full_name: str
    role: UserRole
    is_superadmin: bool


@dataclass(frozen=True)
class TenantContext:
    """Identity facts every tenant-scoped query needs for filtering."""

    tenant_id: int
    user_id: int
    role: UserRole


async def get_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenClaims:
    """Decode the Bearer token from the Authorization header.

    Raises 401 when the header is missing or the token is invalid.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="not authenticated", headers=_AUTH_HEADERS)
    try:
        return decode_token(
            credentials.credentials,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
    except TokenError as exc:
        raise HTTPException(status_code=401, detail="invalid token", headers=_AUTH_HEADERS) from exc


ClaimsDep = Annotated[TokenClaims, Depends(get_claims)]


async def get_current_user(
    claims: ClaimsDep,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CurrentUser:
    """Resolve the token claims to an active database user.

    The user's current database state wins over stale token claims (e.g. a
    role revoked after issuance). Raises 401 when the user is missing,
    inactive, or the token's tenant no longer matches.
    """
    user = await session.get(User, int(claims.sub))
    if user is None or not user.is_active or user.tenant_id != claims.tenant_id:
        raise HTTPException(status_code=401, detail="invalid token", headers=_AUTH_HEADERS)
    return CurrentUser(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_superadmin=user.is_superadmin,
    )


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require_roles(*roles: UserRole):
    """Dependency factory: allow only the given roles (or a superadmin)."""

    async def checker(user: CurrentUserDep) -> CurrentUser:
        if not user.is_superadmin and user.role not in roles:
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return checker


async def get_tenant_context(user: CurrentUserDep) -> TenantContext:
    """Authenticated tenant context for tenant-scoped queries."""
    return TenantContext(tenant_id=user.tenant_id, user_id=user.id, role=user.role)
