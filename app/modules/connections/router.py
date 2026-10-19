"""Per-user connections API: owner-scoped CRUD plus admin listing and revoke.

HTTP shape (one place, :data:`_HTTP_STATUS`):

============================================= ==================== ==================
route                                         meaning              access
============================================= ==================== ==================
``GET /api/v1/connections``                   own rows             any authenticated
``GET /api/v1/connections/tenant``            tenant-wide,         owner/admin
                                              metadata only
``POST /api/v1/connections``                  create-or-update own any authenticated
``GET /api/v1/connections/{id}``              one of own rows      any authenticated
``DELETE /api/v1/connections/{id}``           delete own row       any authenticated
``DELETE /api/v1/connections/{id}/revoke``    revoke any tenant    owner/admin
                                              row
============================================= ==================== ==================

``/tenant`` is declared before ``/{connection_id}`` on purpose: Starlette
matches routes in registration order, and the literal path would otherwise be
swallowed by the int parameter (422 instead of the admin listing).

Rules: credentials are write-only — accepted on POST (and never echoed), and
no response model in this module has a credentials field, so secrets cannot
leave through serialization. Foreign/unknown ids share one 404
(``connection not found``); an unknown ``module_key`` is 404 ``module not
found`` before any write; a missing/malformed encryption key fails closed
with 500 ``credentials encryption is not configured`` and stores nothing.
Catalog source and settings come from ``app.state`` (same pattern as the
catalog router), so test apps swap them per fixture.

Audit (written in the service): ``connection.created`` / ``connection.updated``
/ ``connection.revoked`` / ``connection.deleted`` — payloads are
``{module_key, provider, status}`` and nothing else.
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import get_session
from app.core.deps import get_settings
from app.modules.catalog.exceptions import ModuleCatalogError, UnknownModuleError
from app.modules.catalog.registry import ModuleCatalog
from app.modules.catalog.router import get_module_catalog
from app.modules.connections.exceptions import (
    ConnectionConflictError,
    ConnectionNotFoundError,
    ConnectionsError,
    CredentialsEncryptionError,
)
from app.modules.connections.models import UserConnection
from app.modules.connections.service import (
    create_or_update,
    delete,
    get_for_user,
    list_for_tenant,
    list_for_user,
    revoke,
)
from app.modules.identity.deps import CurrentUser, get_current_user, require_roles
from app.modules.identity.models import UserRole
from packages.contracts.schemas import (
    ConnectionCreateRequest,
    ConnectionListResponse,
    ConnectionResponse,
)

router = APIRouter(prefix="/api/v1/connections", tags=["connections"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
AdminUserDep = Annotated[CurrentUser, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ModuleCatalogDep = Annotated[ModuleCatalog, Depends(get_module_catalog)]

_HTTP_STATUS: dict[type[Exception], tuple[int, str]] = {
    UnknownModuleError: (404, "module not found"),
    ConnectionNotFoundError: (404, "connection not found"),
    ConnectionConflictError: (409, "connection already exists"),
    CredentialsEncryptionError: (500, "credentials encryption is not configured"),
}


def _http_error(exc: Exception) -> HTTPException:
    """Translate a service error; unmapped errors are server bugs (500)."""
    status_code, detail = _HTTP_STATUS.get(type(exc), (500, "internal error"))
    return HTTPException(status_code=status_code, detail=detail)


def _credentials_key(settings: Settings) -> str | None:
    """The raw Fernet key, or ``None`` when unset/blank (fail closed downstream)."""
    if settings.credentials_key is None:
        return None
    return settings.credentials_key.get_secret_value() or None


def _response(connection: UserConnection) -> ConnectionResponse:
    """Public view of a connection: no tenant id, no credentials, no ciphertext."""
    return ConnectionResponse(
        id=connection.id,
        user_id=connection.user_id,
        module_key=connection.module_key,
        provider=connection.provider,
        status=connection.status.value,
        metadata=dict(connection.metadata_ or {}),
        last_health_at=connection.last_health_at,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


def _list_response(connections: list[UserConnection]) -> ConnectionListResponse:
    return ConnectionListResponse(connections=[_response(item) for item in connections])


@router.get("", response_model=ConnectionListResponse)
async def list_own_connections(session: SessionDep, user: CurrentUserDep) -> ConnectionListResponse:
    """The caller's own connections, newest first (every role)."""
    rows = await list_for_user(session, tenant_id=user.tenant_id, user_id=user.id)
    return _list_response(rows)


@router.get("/tenant", response_model=ConnectionListResponse)
async def list_tenant_connections(
    session: SessionDep, admin: AdminUserDep
) -> ConnectionListResponse:
    """Every connection of the tenant — metadata only, owner/admin."""
    rows = await list_for_tenant(session, tenant_id=admin.tenant_id)
    return _list_response(rows)


@router.post("", response_model=ConnectionResponse)
async def create_or_update_connection(
    payload: ConnectionCreateRequest,
    session: SessionDep,
    user: CurrentUserDep,
    catalog: ModuleCatalogDep,
    settings: SettingsDep,
) -> ConnectionResponse:
    """Create or update the caller's own connection; credentials never echo."""
    credentials = payload.credentials
    if isinstance(credentials, dict):
        # One canonical serialized form for dict credentials (still write-only).
        credentials = json.dumps(
            credentials, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
    try:
        connection = await create_or_update(
            session,
            catalog=catalog,
            credentials_key=_credentials_key(settings),
            caller=user,
            module_key=payload.module_key,
            provider=payload.provider,
            credentials=credentials,
            metadata=payload.metadata,
        )
    except (ModuleCatalogError, ConnectionsError) as exc:
        raise _http_error(exc) from exc
    return _response(connection)


@router.get("/{connection_id}", response_model=ConnectionResponse)
async def get_own_connection(
    connection_id: int, session: SessionDep, user: CurrentUserDep
) -> ConnectionResponse:
    """One of the caller's own connections; foreign and unknown ids are the same 404."""
    try:
        connection = await get_for_user(
            session, tenant_id=user.tenant_id, user_id=user.id, connection_id=connection_id
        )
    except ConnectionsError as exc:
        raise _http_error(exc) from exc
    return _response(connection)


@router.delete("/{connection_id}", status_code=204)
async def delete_own_connection(
    connection_id: int, session: SessionDep, user: CurrentUserDep
) -> None:
    """Delete one of the caller's own connections (204, empty body)."""
    try:
        await delete(session, caller=user, connection_id=connection_id)
    except ConnectionsError as exc:
        raise _http_error(exc) from exc


@router.delete("/{connection_id}/revoke", response_model=ConnectionResponse)
async def revoke_connection(
    connection_id: int, session: SessionDep, admin: AdminUserDep
) -> ConnectionResponse:
    """Revoke any connection of the tenant (owner/admin); destroys the secret."""
    try:
        connection = await revoke(session, caller=admin, connection_id=connection_id)
    except ConnectionsError as exc:
        raise _http_error(exc) from exc
    return _response(connection)
