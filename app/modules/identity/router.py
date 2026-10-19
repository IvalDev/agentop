"""Auth API: login, token introspection, current user, and RBAC probe."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import get_session
from app.core.deps import get_settings
from app.modules.identity.deps import (
    CurrentUser,
    get_claims,
    get_current_user,
    require_roles,
)
from app.modules.identity.models import UserRole
from app.modules.identity.security import TokenClaims, create_access_token
from app.modules.identity.service import authenticate
from packages.contracts.schemas import (
    AdminProbeResponse,
    LoginRequest,
    LoginResponse,
    MeResponse,
    TokenIntrospectResponse,
    UserPayload,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ClaimsDep = Annotated[TokenClaims, Depends(get_claims)]
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
AdminUserDep = Annotated[CurrentUser, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))]


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> LoginResponse:
    """Authenticate email/password against a tenant slug and issue a token.

    All failures return the same 401 body ("invalid credentials") to avoid
    user or tenant enumeration.
    """
    user = await authenticate(session, payload.email, payload.password, payload.tenant_slug)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = create_access_token(
        sub=str(user.id),
        tenant_id=user.tenant_id,
        role=user.role.value,
        expires_minutes=settings.access_token_expire_minutes,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserPayload(
            id=user.id,
            email=user.email,
            role=user.role.value,
            tenant_id=user.tenant_id,
        ),
    )


@router.post("/token/introspect", response_model=TokenIntrospectResponse)
async def token_introspect(claims: ClaimsDep) -> TokenClaims:
    """Return the validated claims of the caller's token (debugging/tests)."""
    return claims


@router.get("/me", response_model=MeResponse)
async def me(user: CurrentUserDep) -> MeResponse:
    """Return the authenticated user's profile."""
    return MeResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role.value,
        tenant_id=user.tenant_id,
    )


@router.get("/admin-probe", response_model=AdminProbeResponse)
async def admin_probe(_user: AdminUserDep) -> AdminProbeResponse:
    """Role-gate probe: only owner/admin pass; used to verify RBAC."""
    return AdminProbeResponse(status="ok")
