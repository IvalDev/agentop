"""Auth API: login, refresh, logout, introspection, current user, RBAC probe.

Persistent sessions (T2): login and refresh keep the access token short-lived
(``access_token_expire_minutes``) while the refresh token travels in an
httpOnly, SameSite=strict cookie scoped to this router's path. The server
stores only its SHA-256 digest and rotates it on every use; replaying a
rotated token revokes the whole session family.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
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
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import TokenClaims, create_access_token
from app.modules.identity.service import (
    authenticate,
    create_refresh_session,
    revoke_refresh_session,
    rotate_refresh_session,
)
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

#: Refresh cookie: invisible to JavaScript, strict same-site, and sent only to
#: the auth endpoints (login/refresh/logout) thanks to its path scope.
REFRESH_COOKIE_NAME = "admin-agent.refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"

_SECONDS_PER_DAY = 24 * 60 * 60


def _refresh_cookie_max_age(settings: Settings) -> int:
    return settings.refresh_token_expire_days * _SECONDS_PER_DAY


def _set_refresh_cookie(response: Response, raw_token: str, settings: Settings) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=raw_token,
        max_age=_refresh_cookie_max_age(settings),
        path=REFRESH_COOKIE_PATH,
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="strict",
    )


def _invalid_refresh_response() -> JSONResponse:
    """Uniform 401 for every bad refresh cookie, clearing it in the client."""
    response = JSONResponse({"detail": "invalid refresh token"}, status_code=401)
    _clear_refresh_cookie(response)
    return response


def _login_response(user: User, settings: Settings) -> LoginResponse:
    """The login/refresh reply: a fresh access token plus the public profile."""
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


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> LoginResponse:
    """Authenticate credentials, issue an access token, and start a session.

    All failures return the same 401 body ("invalid credentials") to avoid
    user or tenant enumeration. Success also sets the refresh cookie.
    """
    user = await authenticate(session, payload.email, payload.password, payload.tenant_slug)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    _session, raw_token = await create_refresh_session(
        session, user, expires_days=settings.refresh_token_expire_days
    )
    await session.commit()
    _set_refresh_cookie(response, raw_token, settings)
    return _login_response(user, settings)


@router.post("/refresh", response_model=LoginResponse)
async def refresh(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """Rotate the refresh cookie and issue a fresh access token.

    Every rejection (missing, unknown, expired, revoked, or deactivated user)
    answers the same 401 and clears the cookie. Replaying a rotated token is
    theft: the whole family is revoked before answering.
    """
    raw_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_token:
        rotation = await rotate_refresh_session(
            session, raw_token, expires_days=settings.refresh_token_expire_days
        )
        if rotation is not None:
            user, new_raw_token = rotation
            await session.commit()
            _set_refresh_cookie(response, new_raw_token, settings)
            return _login_response(user, settings)
        await session.commit()  # persists the family revocation, if any
    return _invalid_refresh_response()


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, session: SessionDep) -> None:
    """Revoke only the presented session (other devices keep theirs)."""
    raw_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_token and await revoke_refresh_session(session, raw_token):
        await session.commit()
    _clear_refresh_cookie(response)


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
