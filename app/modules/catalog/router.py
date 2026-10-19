"""Module catalog API: read the catalog, activate modules for the tenant.

HTTP shape (one place, :data:`_HTTP_STATUS`):

=================================== ======================== ==============
route                               meaning                  access
=================================== ======================== ==============
``GET /api/v1/modules``             catalog + this tenant's  any authenticated
                                    ``active`` flags
``POST /api/v1/modules/{key}/activate``   set the flag on    owner/admin
``DELETE /api/v1/modules/{key}/activate`` remove the          owner/admin
                                    activation
=================================== ======================== ==============

``DELETE`` targets ``/activate``: the activation is the sub-resource being
removed, so one path carries both verbs of one state (deactivation is the
absence of an activation, not a second resource). Both mutations answer
``200`` with the resulting ``{key, active}`` state — repeats included, they
never duplicate rows — and ``404 module not found`` when the catalog does not
declare the key. Reads never mutate, so any role (viewer included) sees the
same catalog the admin panel edits: modules are a tenant-wide switch,
configured by owner/admin, visible to everyone.

Catalog source: ``app.state.module_catalog`` (the process-wide default in
production, replaced by test apps exactly like ``app.state.tool_registry``).
Every state change is audited in the service (``module.activated`` /
``module.deactivated``); no route writes audit events itself.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.modules.catalog.exceptions import ModuleCatalogError, UnknownModuleError
from app.modules.catalog.registry import ModuleCatalog
from app.modules.catalog.service import (
    CatalogEntry,
    activate,
    deactivate,
    list_catalog_for_tenant,
)
from app.modules.identity.deps import CurrentUser, get_current_user, require_roles
from app.modules.identity.models import UserRole
from packages.contracts.schemas import (
    ModuleActivationResponse,
    ModuleListResponse,
    ModuleResponse,
)

router = APIRouter(prefix="/api/v1/modules", tags=["modules"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
AdminUserDep = Annotated[CurrentUser, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))]


def get_module_catalog(request: Request) -> ModuleCatalog:
    """Return the catalog attached to ``app.state.module_catalog``."""
    return request.app.state.module_catalog


ModuleCatalogDep = Annotated[ModuleCatalog, Depends(get_module_catalog)]

_HTTP_STATUS: dict[type[ModuleCatalogError], tuple[int, str]] = {
    UnknownModuleError: (404, "module not found"),
}


def _http_error(exc: ModuleCatalogError) -> HTTPException:
    """Translate a service error; unmapped errors are server bugs (500)."""
    status_code, detail = _HTTP_STATUS.get(type(exc), (500, "internal error"))
    return HTTPException(status_code=status_code, detail=detail)


def _module_response(entry: CatalogEntry) -> ModuleResponse:
    """Public view of one catalog entry (onboarding steps stay server-side)."""
    manifest = entry.manifest
    return ModuleResponse(
        key=manifest.key,
        name=manifest.name,
        description=manifest.description,
        version=manifest.version,
        permissions=list(manifest.permissions),
        tools=list(manifest.tools),
        active=entry.active,
    )


@router.get("", response_model=ModuleListResponse)
async def list_modules(
    session: SessionDep, user: CurrentUserDep, catalog: ModuleCatalogDep
) -> ModuleListResponse:
    """Return the catalog with the caller's own tenant activation flags."""
    entries = await list_catalog_for_tenant(session, user.tenant_id, catalog)
    return ModuleListResponse(modules=[_module_response(entry) for entry in entries])


@router.post("/{key}/activate", response_model=ModuleActivationResponse)
async def activate_module(
    key: str, session: SessionDep, catalog: ModuleCatalogDep, caller: AdminUserDep
) -> ModuleActivationResponse:
    """Activate a catalog module for the caller's tenant (idempotent)."""
    try:
        activation = await activate(session, catalog, caller, key)
    except ModuleCatalogError as exc:
        raise _http_error(exc) from exc
    return ModuleActivationResponse(key=activation.module_key, active=activation.active)


@router.delete("/{key}/activate", response_model=ModuleActivationResponse)
async def deactivate_module(
    key: str, session: SessionDep, catalog: ModuleCatalogDep, caller: AdminUserDep
) -> ModuleActivationResponse:
    """Remove the tenant's activation; already inactive is the same 200."""
    try:
        activation = await deactivate(session, catalog, caller, key)
    except ModuleCatalogError as exc:
        raise _http_error(exc) from exc
    # The resulting state is inactive whether a row flipped or none existed.
    active = activation is not None and activation.active
    return ModuleActivationResponse(key=key, active=active)
