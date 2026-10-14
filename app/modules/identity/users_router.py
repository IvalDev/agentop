"""Team management API: owner/admin manage the users of their own tenant.

Every route needs the owner or admin role (403 ``forbidden`` otherwise) and
acts on the caller's tenant, taken from the token. Rules live in
:mod:`app.modules.identity.users_service`.

HTTP mapping (one place, :data:`_HTTP_STATUS`):

======================= ===================================================
service error           HTTP
======================= ===================================================
``UserNotFoundError``   404 ``user not found`` (unknown *and* other-tenant ids)
``UserForbiddenError``  403 ``forbidden`` (owner-only change asked by an admin)
``UserConflictError``   409 with the rule as ``detail`` (duplicate email,
                        self change, last active owner)
======================= ===================================================
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.modules.identity.deps import CurrentUser, require_roles
from app.modules.identity.models import User, UserRole
from app.modules.identity.users_service import (
    UserAdminError,
    UserConflictError,
    UserForbiddenError,
    UserNotFoundError,
    change_role,
    create_user,
    list_users,
    set_active,
)
from packages.contracts.schemas import (
    TeamUserCreateRequest,
    TeamUserListResponse,
    TeamUserResponse,
    TeamUserRoleRequest,
)

router = APIRouter(prefix="/api/v1/users", tags=["users"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminUserDep = Annotated[CurrentUser, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))]

_HTTP_STATUS: dict[type[UserAdminError], int] = {
    UserNotFoundError: 404,
    UserForbiddenError: 403,
    UserConflictError: 409,
}


def _http_error(exc: UserAdminError) -> HTTPException:
    """Translate a service error; unmapped errors are server bugs (500)."""
    status_code = _HTTP_STATUS.get(type(exc), 500)
    detail = "internal error" if status_code == 500 else str(exc)
    return HTTPException(status_code=status_code, detail=detail)


def _response(user: User) -> TeamUserResponse:
    """Public view of a user (never the tenant id or the password hash)."""
    return TeamUserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role.value,
        is_active=user.is_active,
        created_at=user.created_at,
    )


@router.get("", response_model=TeamUserListResponse)
async def list_team(session: SessionDep, caller: AdminUserDep) -> TeamUserListResponse:
    """Return every user of the caller's tenant, oldest first."""
    users = await list_users(session, caller)
    return TeamUserListResponse(users=[_response(user) for user in users])


@router.post("", response_model=TeamUserResponse, status_code=201)
async def create_member(
    payload: TeamUserCreateRequest, session: SessionDep, caller: AdminUserDep
) -> TeamUserResponse:
    """Create an active user in the caller's tenant (only an owner creates an owner)."""
    try:
        user = await create_user(
            session,
            caller,
            email=payload.email,
            full_name=payload.full_name,
            role=UserRole(payload.role),
            password=payload.password,
        )
    except UserAdminError as exc:
        raise _http_error(exc) from exc
    return _response(user)


@router.put("/{user_id}/role", response_model=TeamUserResponse)
async def update_role(
    user_id: int, payload: TeamUserRoleRequest, session: SessionDep, caller: AdminUserDep
) -> TeamUserResponse:
    """Set a user's role; the same role is a no-op."""
    try:
        return _response(await change_role(session, caller, user_id, UserRole(payload.role)))
    except UserAdminError as exc:
        raise _http_error(exc) from exc


@router.post("/{user_id}/deactivate", response_model=TeamUserResponse)
async def deactivate(user_id: int, session: SessionDep, caller: AdminUserDep) -> TeamUserResponse:
    """Block a user's login and existing tokens; an inactive user is a no-op."""
    try:
        return _response(await set_active(session, caller, user_id, active=False))
    except UserAdminError as exc:
        raise _http_error(exc) from exc


@router.post("/{user_id}/reactivate", response_model=TeamUserResponse)
async def reactivate(user_id: int, session: SessionDep, caller: AdminUserDep) -> TeamUserResponse:
    """Let a deactivated user log in again; an active user is a no-op."""
    try:
        return _response(await set_active(session, caller, user_id, active=True))
    except UserAdminError as exc:
        raise _http_error(exc) from exc
