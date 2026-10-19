"""Per-user connections: create-or-update, scoped reads, revoke, admin replace.

The secret boundary (T3)
-------------------------
This module is the only place that *encrypts* credentials, and it never
*decrypts* them: plaintext enters at create/replace and never leaves again —
not in a return value, not in an audit payload, not in a log line. Audit
events carry exactly ``{module_key, provider, status}``. Identifier arguments
are keyword-only, so tenant and user ids can never be swapped by accident.

Rules enforced here (the router only translates errors to HTTP):

- ``module_key`` must exist in ``catalog`` — ``UnknownModuleError`` before any
  database access (router: 404);
- storing credentials without a usable ``credentials_key`` raises
  ``CredentialsEncryptionError`` *before* any write (fail closed: no key, no
  connection — never plaintext at rest);
- owner-scoped operations filter ``tenant_id`` **and** ``user_id``; admin
  operations filter ``tenant_id``; foreign and unknown ids all raise
  ``ConnectionNotFoundError`` with one message (router: 404), so existence
  never leaks across scopes.

Status semantics of a stored row:

- created without credentials → ``pending``;
- credentials stored (create or replace) → ``connected`` — which also
  re-activates a revoked row: storing fresh credentials *is* the reconnection;
- :func:`set_status` flips ``expired``/``failed`` for the T4 health seam and
  audits nothing (health is system state, not a user action);
- :func:`revoke` → ``revoked`` **and destroys** ``credentials_encrypted``:
  the secret is unrecoverable, only a fresh create can reconnect. An
  already-revoked row with no blob is a no-op (no second event);
- :func:`delete` removes the row and audits its pre-delete state.

Each function commits its own unit of work; audit events go through
:func:`stage_event <app.modules.audit.service.stage_event>` — staged on the
caller's transaction and committed atomically with it, same as the catalog
service (SQLite's single writer makes an independent audit connection
deadlock on a file database).
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CredentialsKeyFormatError, encrypt_credentials
from app.modules.audit.service import stage_event
from app.modules.catalog.registry import ModuleCatalog
from app.modules.connections.exceptions import (
    ConnectionConflictError,
    ConnectionNotFoundError,
    CredentialsEncryptionError,
)
from app.modules.connections.models import ConnectionStatus, UserConnection
from app.modules.identity.deps import CurrentUser

_RESOURCE = "connection"
_NOT_FOUND = "connection not found"
_NO_KEY = "credentials encryption is not configured"


def _encrypt(credentials_key: str | None, plaintext: str) -> str:
    """Encrypt ``plaintext``; a missing or malformed key fails closed.

    Callers invoke this BEFORE touching the session, so a refused write never
    leaves a half-created row behind. One fixed message for both cases: a
    malformed key can only be a deployment bug (settings validated it), and
    its detail must not echo the key either.
    """
    if credentials_key is None:
        raise CredentialsEncryptionError(_NO_KEY)
    try:
        return encrypt_credentials(credentials_key, plaintext)
    except CredentialsKeyFormatError as exc:
        raise CredentialsEncryptionError(_NO_KEY) from exc


async def _load_natural_key(
    session: AsyncSession,
    *,
    tenant_id: int,
    user_id: int,
    module_key: str,
    provider: str,
) -> UserConnection | None:
    """Return the row for one natural key, or ``None`` when it does not exist."""
    result = await session.execute(
        select(UserConnection).where(
            UserConnection.tenant_id == tenant_id,
            UserConnection.user_id == user_id,
            UserConnection.module_key == module_key,
            UserConnection.provider == provider,
        )
    )
    return result.scalar_one_or_none()


async def _load_scoped(
    session: AsyncSession,
    *,
    tenant_id: int,
    connection_id: int,
    user_id: int | None = None,
) -> UserConnection | None:
    """Return the row within one tenant (optionally narrowed to one user)."""
    query = select(UserConnection).where(
        UserConnection.id == connection_id,
        UserConnection.tenant_id == tenant_id,
    )
    if user_id is not None:
        query = query.where(UserConnection.user_id == user_id)
    return (await session.execute(query)).scalar_one_or_none()


async def _require_owned(
    session: AsyncSession, *, tenant_id: int, user_id: int, connection_id: int
) -> UserConnection:
    """Owner scope: the caller's own row, or one ``ConnectionNotFoundError``."""
    row = await _load_scoped(
        session, tenant_id=tenant_id, connection_id=connection_id, user_id=user_id
    )
    if row is None:
        raise ConnectionNotFoundError(_NOT_FOUND)
    return row


async def _require_in_tenant(
    session: AsyncSession, *, tenant_id: int, connection_id: int
) -> UserConnection:
    """Admin scope: any row of this tenant, or one ``ConnectionNotFoundError``."""
    row = await _load_scoped(session, tenant_id=tenant_id, connection_id=connection_id)
    if row is None:
        raise ConnectionNotFoundError(_NOT_FOUND)
    return row


async def _refresh_after_flush(session: AsyncSession, row: UserConnection) -> None:
    """Reload ``row`` so its server-managed ``updated_at`` is hydrated again.

    ``TimestampMixin.updated_at`` has a server ``onupdate``: flushing an
    UPDATE leaves that column expired, and a later *synchronous* attribute
    access (the router serializing ``ConnectionResponse``) would attempt IO
    outside the greenlet — ``MissingGreenlet``. Reloading here, inside the
    async session before commit, hands the caller a fully loaded row.
    """
    await session.refresh(row)


async def _persist_state_event(
    session: AsyncSession, caller: CurrentUser, action: str, row: UserConnection
) -> None:
    """Stage one connection-change event on the caller's transaction.

    Staged, not independently committed: the caller commits right after, so
    action and audit share SQLite's single writer atomically instead of
    deadlocking on a second connection. The payload is the public triple
    only — ``row`` may be an instance already scheduled for deletion; its
    loaded attributes are what gets audited.
    """
    stage_event(
        session,
        tenant_id=caller.tenant_id,
        action=action,
        actor_user_id=caller.id,
        resource_type=_RESOURCE,
        resource_id=str(row.id),
        payload={
            "module_key": row.module_key,
            "provider": row.provider,
            "status": row.status.value,
        },
    )


async def create_or_update(
    session: AsyncSession,
    *,
    catalog: ModuleCatalog,
    credentials_key: str | None,
    caller: CurrentUser,
    module_key: str,
    provider: str,
    credentials: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> UserConnection:
    """Create or update the caller's own connection (natural-key upsert).

    ``module_key`` must be in ``catalog`` — checked first, before any write.
    ``credentials`` is write-only: when given it is encrypted and the status
    becomes ``connected``; when omitted a new row stays ``pending`` and an
    existing row keeps both its stored secret and its status. ``metadata``
    replaces the display dict when given and is kept otherwise.
    """
    catalog.get(module_key)
    encrypted = _encrypt(credentials_key, credentials) if credentials is not None else None
    row = await _load_natural_key(
        session,
        tenant_id=caller.tenant_id,
        user_id=caller.id,
        module_key=module_key,
        provider=provider,
    )
    is_new = row is None
    if row is None:
        row = UserConnection(
            tenant_id=caller.tenant_id,
            user_id=caller.id,
            module_key=module_key,
            provider=provider,
            status=ConnectionStatus.PENDING,
            metadata_=dict(metadata or {}),
        )
        session.add(row)
    elif metadata is not None:
        row.metadata_ = dict(metadata)
    if encrypted is not None:
        row.credentials_encrypted = encrypted
        row.status = ConnectionStatus.CONNECTED
    await session.flush()
    if not is_new:
        await _refresh_after_flush(session, row)
    await _persist_state_event(
        session, caller, "connection.created" if is_new else "connection.updated", row
    )
    await session.commit()
    return row


async def list_for_user(
    session: AsyncSession, *, tenant_id: int, user_id: int
) -> list[UserConnection]:
    """Return the user's connections, newest first (never other users/tenants)."""
    rows = await session.scalars(
        select(UserConnection)
        .where(
            UserConnection.tenant_id == tenant_id,
            UserConnection.user_id == user_id,
        )
        .order_by(UserConnection.created_at.desc(), UserConnection.id.desc())
    )
    return list(rows)


async def get_for_user(
    session: AsyncSession, *, tenant_id: int, user_id: int, connection_id: int
) -> UserConnection:
    """Return one of the caller's own connections, or raise ``ConnectionNotFoundError``."""
    return await _require_owned(
        session, tenant_id=tenant_id, user_id=user_id, connection_id=connection_id
    )


async def list_for_tenant(session: AsyncSession, *, tenant_id: int) -> list[UserConnection]:
    """Return every connection of one tenant (admin view), newest first.

    Rows only — the router serializes them through a contract with no
    credentials field, so the admin listing is metadata by construction.
    """
    rows = await session.scalars(
        select(UserConnection)
        .where(UserConnection.tenant_id == tenant_id)
        .order_by(UserConnection.created_at.desc(), UserConnection.id.desc())
    )
    return list(rows)


async def get_for_tenant(
    session: AsyncSession, *, tenant_id: int, connection_id: int
) -> UserConnection:
    """Return any connection of one tenant (admin scope), or raise ``ConnectionNotFoundError``."""
    return await _require_in_tenant(session, tenant_id=tenant_id, connection_id=connection_id)


async def set_status(
    session: AsyncSession,
    *,
    tenant_id: int,
    connection_id: int,
    status: ConnectionStatus,
    user_id: int | None = None,
) -> UserConnection:
    """Flip a connection's status — the T4 health seam (expired/failed).

    Tenant-scoped always; pass ``user_id`` to narrow to one owner. Foreign or
    unknown ids raise ``ConnectionNotFoundError``. Audits nothing: a health
    transition is system state, not a user or admin action (the trail covers
    created/updated/revoked/deleted).
    """
    row = await _load_scoped(
        session, tenant_id=tenant_id, connection_id=connection_id, user_id=user_id
    )
    if row is None:
        raise ConnectionNotFoundError(_NOT_FOUND)
    row.status = status
    await session.flush()
    await _refresh_after_flush(session, row)
    await session.commit()
    return row


async def revoke(
    session: AsyncSession, *, caller: CurrentUser, connection_id: int
) -> UserConnection:
    """Revoke any connection of the caller's tenant (owner/admin behind the router).

    Sets ``revoked`` and destroys ``credentials_encrypted`` — the secret is
    gone; only a fresh create can reconnect. Already revoked with no blob is
    a no-op: the same row back, no second event. Foreign/unknown ids raise
    ``ConnectionNotFoundError``.
    """
    row = await _require_in_tenant(session, tenant_id=caller.tenant_id, connection_id=connection_id)
    if row.status is ConnectionStatus.REVOKED and row.credentials_encrypted is None:
        return row
    row.status = ConnectionStatus.REVOKED
    row.credentials_encrypted = None
    await session.flush()
    await _refresh_after_flush(session, row)
    await _persist_state_event(session, caller, "connection.revoked", row)
    await session.commit()
    return row


async def delete(session: AsyncSession, *, caller: CurrentUser, connection_id: int) -> None:
    """Delete one of the caller's OWN connections (tenant + user scoped).

    Foreign and unknown ids raise the same ``ConnectionNotFoundError``; the
    audit event captures the row's state *before* the delete.
    """
    row = await _require_owned(
        session, tenant_id=caller.tenant_id, user_id=caller.id, connection_id=connection_id
    )
    payload = {"module_key": row.module_key, "provider": row.provider, "status": row.status.value}
    resource_id = str(row.id)
    await session.delete(row)
    await session.flush()
    stage_event(
        session,
        tenant_id=caller.tenant_id,
        action="connection.deleted",
        actor_user_id=caller.id,
        resource_type=_RESOURCE,
        resource_id=resource_id,
        payload=payload,
    )
    await session.commit()


async def replace(
    session: AsyncSession,
    *,
    catalog: ModuleCatalog,
    credentials_key: str | None,
    caller: CurrentUser,
    connection_id: int,
    module_key: str,
    provider: str,
    metadata: dict[str, Any],
    credentials: str | None = None,
) -> UserConnection:
    """Admin full-replace of one tenant connection's display fields (D5 ops path).

    ``module_key`` (catalog-validated), ``provider``, and ``metadata`` always
    replace. ``credentials`` follows the create-or-update rule: given →
    encrypted and ``connected``; omitted → the stored secret and status stay.
    A natural-key collision with a *different* row of the same user raises
    ``ConnectionConflictError`` — checked before any mutation, so the failed
    replace leaves the session clean. Foreign/unknown ids raise
    ``ConnectionNotFoundError``.
    """
    catalog.get(module_key)
    encrypted = _encrypt(credentials_key, credentials) if credentials is not None else None
    row = await _require_in_tenant(session, tenant_id=caller.tenant_id, connection_id=connection_id)
    clash = await _load_natural_key(
        session,
        tenant_id=caller.tenant_id,
        user_id=row.user_id,
        module_key=module_key,
        provider=provider,
    )
    if clash is not None and clash.id != row.id:
        raise ConnectionConflictError("connection already exists")
    row.module_key = module_key
    row.provider = provider
    row.metadata_ = dict(metadata)
    if encrypted is not None:
        row.credentials_encrypted = encrypted
        row.status = ConnectionStatus.CONNECTED
    await session.flush()
    await _refresh_after_flush(session, row)
    await _persist_state_event(session, caller, "connection.updated", row)
    await session.commit()
    return row
