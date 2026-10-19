"""Team management: list, create, re-role, deactivate, and reactivate users.

Every function acts on the caller's own tenant only (``caller.tenant_id``,
which comes from the token, never from the client). The router gates every
route to owner/admin; this module enforces the rules that depend on the
target:

- A user of another tenant is indistinguishable from a missing one
  (:class:`UserNotFoundError`), checked before any other rule.
- Nobody changes their own role or active state (:class:`UserConflictError`).
- Only an owner creates an owner, promotes to or demotes from ``owner``, or
  deactivates or reactivates an owner; an admin gets
  :class:`UserForbiddenError`. A superadmin passes this check, as it passes
  every in-tenant role check.
- The tenant always keeps at least one active owner
  (:class:`UserConflictError`). The tenant's active owner rows are locked
  (``SELECT ... FOR UPDATE`` on PostgreSQL) before the count, so two
  concurrent requests cannot each remove "the other" owner.
- Setting a role or state the user already has is a no-op: no write, no event.

Audit: each change stages ``user.created``, ``user.role_changed``,
``user.deactivated``, or ``user.reactivated`` in the transaction that commits
it, so an event exists iff its change does. A refusal by the guards above is
persisted at once as ``user.denied``. No event ever carries the password or
its hash. Each function commits its own unit of work.
"""

from typing import NoReturn

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import persist_event, stage_event
from app.modules.identity.deps import CurrentUser, TenantContext
from app.modules.identity.models import User, UserRole
from app.modules.identity.security import hash_password
from app.modules.identity.service import scoped_query

_RESOURCE = "user"


class UserAdminError(Exception):
    """Base class for every team-management error."""


class UserNotFoundError(UserAdminError):
    """No user with that id exists in the caller's tenant (other tenants included)."""


class UserForbiddenError(UserAdminError):
    """The caller's role cannot perform this change (owner-only rules)."""


class UserConflictError(UserAdminError):
    """The change breaks a team rule or collides with an existing user."""


def _context(caller: CurrentUser) -> TenantContext:
    return TenantContext(tenant_id=caller.tenant_id, user_id=caller.id, role=caller.role)


def _manages_owners(caller: CurrentUser) -> bool:
    return caller.is_superadmin or caller.role is UserRole.OWNER


async def _load(session: AsyncSession, caller: CurrentUser, user_id: int) -> User:
    """Fetch a fresh copy of a user of the caller's tenant, else not-found."""
    result = await session.execute(
        scoped_query(User, _context(caller))
        .where(User.id == user_id)
        .execution_options(populate_existing=True)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise UserNotFoundError("user not found")
    return user


async def _refuse(
    session: AsyncSession,
    caller: CurrentUser,
    operation: str,
    target_id: int | None,
    error: UserForbiddenError | UserConflictError,
) -> NoReturn:
    """Persist ``user.denied`` for ``operation``, then raise ``error``."""
    if isinstance(error, UserForbiddenError):
        result = {"error": "forbidden"}
    else:
        result = {"error": "conflict", "detail": str(error)}
    await persist_event(
        session,
        tenant_id=caller.tenant_id,
        action="user.denied",
        actor_user_id=caller.id,
        resource_type=_RESOURCE,
        resource_id=None if target_id is None else str(target_id),
        payload={"operation": operation},
        result=result,
    )
    raise error


async def _guard_target(
    session: AsyncSession, caller: CurrentUser, operation: str, target: User, new_role: UserRole
) -> None:
    """Apply the self and owner-only rules to a change of ``target``."""
    if target.id == caller.id:
        if operation == "change_role":
            detail = "cannot change your own role"
        else:
            detail = f"cannot {operation} yourself"
        await _refuse(session, caller, operation, target.id, UserConflictError(detail))
    touches_owner = UserRole.OWNER in (target.role, new_role)
    if touches_owner and not _manages_owners(caller):
        await _refuse(session, caller, operation, target.id, UserForbiddenError("forbidden"))


async def _keep_an_active_owner(
    session: AsyncSession, caller: CurrentUser, operation: str, target: User
) -> None:
    """Refuse when ``target`` is the tenant's last active owner (rows locked first)."""
    owner_ids = await session.scalars(
        select(User.id)
        .where(
            User.tenant_id == caller.tenant_id,
            User.role == UserRole.OWNER,
            User.is_active.is_(True),
        )
        .with_for_update()
    )
    if not set(owner_ids) - {target.id}:
        error = UserConflictError("tenant must keep at least one active owner")
        await _refuse(session, caller, operation, target.id, error)


def _stage(
    session: AsyncSession,
    caller: CurrentUser,
    action: str,
    target: User,
    payload: dict[str, str] | None = None,
) -> None:
    """Stage one change event of ``target`` (commits with the change)."""
    stage_event(
        session,
        tenant_id=caller.tenant_id,
        action=action,
        actor_user_id=caller.id,
        resource_type=_RESOURCE,
        resource_id=str(target.id),
        payload=payload,
    )


async def _commit(session: AsyncSession, user: User) -> User:
    """Commit and reload ``user`` (server-maintained columns included)."""
    await session.commit()
    await session.refresh(user)
    return user


async def list_users(session: AsyncSession, caller: CurrentUser) -> list[User]:
    """Return every user of the caller's tenant, oldest first."""
    result = await session.execute(scoped_query(User, _context(caller)).order_by(User.id))
    return list(result.scalars())


async def create_user(
    session: AsyncSession,
    caller: CurrentUser,
    *,
    email: str,
    full_name: str,
    role: UserRole,
    password: str,
) -> User:
    """Create an active user in the caller's tenant.

    ``email`` must already be normalized (trimmed, lowercased). Raises
    :class:`UserForbiddenError` when an admin asks for an owner and
    :class:`UserConflictError` when the email is taken in this tenant.
    """
    if role is UserRole.OWNER and not _manages_owners(caller):
        await _refuse(session, caller, "create", None, UserForbiddenError("forbidden"))
    taken = await session.scalar(
        scoped_query(User, _context(caller))
        .where(func.lower(User.email) == email)
        .with_only_columns(func.count())
    )
    if taken:
        raise UserConflictError("email already registered")
    user = User(
        tenant_id=caller.tenant_id,
        email=email,
        full_name=full_name,
        hashed_password=hash_password(password),
        role=role,
        is_active=True,
        is_superadmin=False,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:  # a concurrent create won the unique constraint
        await session.rollback()
        raise UserConflictError("email already registered") from exc
    _stage(
        session,
        caller,
        "user.created",
        user,
        {"email": email, "full_name": full_name, "role": role.value},
    )
    return await _commit(session, user)


async def change_role(
    session: AsyncSession, caller: CurrentUser, user_id: int, role: UserRole
) -> User:
    """Give a user of the caller's tenant a new role (see the module rules)."""
    target = await _load(session, caller, user_id)
    await _guard_target(session, caller, "change_role", target, role)
    if target.role is role:
        return target
    if target.role is UserRole.OWNER and target.is_active:
        await _keep_an_active_owner(session, caller, "change_role", target)
    previous = target.role
    target.role = role
    _stage(session, caller, "user.role_changed", target, {"from": previous.value, "to": role.value})
    return await _commit(session, target)


async def set_active(
    session: AsyncSession, caller: CurrentUser, user_id: int, *, active: bool
) -> User:
    """Reactivate (``active=True``) or deactivate a user of the caller's tenant.

    A deactivated user can no longer log in, and every token already issued
    to them is refused on the next request (``get_current_user`` reads
    ``is_active`` from the database).
    """
    operation = "reactivate" if active else "deactivate"
    target = await _load(session, caller, user_id)
    await _guard_target(session, caller, operation, target, target.role)
    if target.is_active is active:
        return target
    if not active and target.role is UserRole.OWNER:
        await _keep_an_active_owner(session, caller, operation, target)
    target.is_active = active
    _stage(session, caller, f"user.{operation}d", target)
    return await _commit(session, target)
