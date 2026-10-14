"""Module activation: read the catalog per tenant and switch modules on/off.

Every function acts on one tenant's rows only (``tenant_id`` comes from the
authenticated principal, never from the client). The router gates the
mutations to owner/admin; this module enforces the data rules:

- a key outside ``catalog`` raises
  :class:`~app.modules.catalog.exceptions.UnknownModuleError` (router: 404)
  before any database access;
- activating an already-active module is a no-op: no row write, no event
  (:func:`activate` returns the untouched row);
- deactivating an inactive or never-activated module writes nothing and
  returns ``None`` — the router still answers the same 200 with
  ``active=false``. Repeats therefore never duplicate rows (the unique
  ``(tenant_id, module_key)`` constraint is the backstop).

Audit: each real state change persists ``module.activated`` /
``module.deactivated`` through
:func:`persist_event <app.modules.audit.service.persist_event>` with
``resource_type="module"``, ``resource_id=<key>``, and a payload of key plus
manifest version — manifests carry no secrets, so neither does the event.
``persist_event`` commits in its own transaction, so the trail survives a
later rollback of the caller's transaction (see the audit service for the
in-memory SQLite caveat in tests). Each function commits its own unit of
work; idempotent repeats audit nothing because nothing changed.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import persist_event
from app.modules.catalog.manifest import ModuleManifest
from app.modules.catalog.models import ModuleActivation
from app.modules.catalog.registry import ModuleCatalog
from app.modules.identity.deps import CurrentUser

_RESOURCE = "module"


@dataclass(frozen=True)
class CatalogEntry:
    """One catalog manifest paired with whether ``tenant_id`` activated it."""

    manifest: ModuleManifest
    active: bool


async def active_module_keys(session: AsyncSession, tenant_id: int) -> set[str]:
    """Return the module keys ``tenant_id`` has active — the T2 gating input.

    Active rows only: deactivated rows and other tenants' rows never appear.
    This helper reads no catalog, so an activation row whose module was later
    removed from the code still counts as active for that tenant; the catalog
    and tool registry stay the authority for what *exists*, this set only
    answers what the tenant switched *on*.
    """
    rows = await session.scalars(
        select(ModuleActivation.module_key).where(
            ModuleActivation.tenant_id == tenant_id,
            ModuleActivation.active.is_(True),
        )
    )
    return set(rows)


async def list_catalog_for_tenant(
    session: AsyncSession, tenant_id: int, catalog: ModuleCatalog
) -> list[CatalogEntry]:
    """Pair every catalog manifest (registration order) with the tenant's flag.

    Modules the tenant never touched come back ``active=False``; there is no
    row to create for a read.
    """
    rows = await session.scalars(
        select(ModuleActivation.module_key).where(
            ModuleActivation.tenant_id == tenant_id,
            ModuleActivation.active.is_(True),
        )
    )
    active_keys = set(rows)
    return [
        CatalogEntry(manifest=manifest, active=manifest.key in active_keys)
        for manifest in catalog.list_manifests()
    ]


async def _load_activation(
    session: AsyncSession, tenant_id: int, key: str
) -> ModuleActivation | None:
    """Return the tenant's row for ``key``, or ``None`` when never activated."""
    result = await session.execute(
        select(ModuleActivation).where(
            ModuleActivation.tenant_id == tenant_id,
            ModuleActivation.module_key == key,
        )
    )
    return result.scalar_one_or_none()


async def _persist_state_event(
    session: AsyncSession, caller: CurrentUser, action: str, manifest: ModuleManifest
) -> None:
    """Write one activation-change event (its own transaction, no secrets)."""
    await persist_event(
        session,
        tenant_id=caller.tenant_id,
        action=action,
        actor_user_id=caller.id,
        resource_type=_RESOURCE,
        resource_id=manifest.key,
        payload={"key": manifest.key, "version": manifest.version},
    )


async def activate(
    session: AsyncSession, catalog: ModuleCatalog, caller: CurrentUser, key: str
) -> ModuleActivation:
    """Activate ``key`` for the caller's tenant (owner/admin behind the router).

    Idempotent: an already-active module is returned untouched — no write,
    no event. Raises ``UnknownModuleError`` for a key outside ``catalog``
    before touching the database. Re-activating a deactivated module flips
    the same row back on (unique per tenant+key) and audits the change.
    """
    manifest = catalog.get(key)
    row = await _load_activation(session, caller.tenant_id, key)
    if row is not None and row.active:
        return row
    if row is None:
        row = ModuleActivation(
            tenant_id=caller.tenant_id,
            module_key=key,
            active=True,
            activated_by_user_id=caller.id,
        )
        session.add(row)
    else:
        row.active = True
        row.activated_by_user_id = caller.id
    await session.flush()
    await _persist_state_event(session, caller, "module.activated", manifest)
    await session.commit()
    return row


async def deactivate(
    session: AsyncSession, catalog: ModuleCatalog, caller: CurrentUser, key: str
) -> ModuleActivation | None:
    """Deactivate ``key`` for the caller's tenant; ``None`` when there was nothing to do.

    Idempotent: when the tenant never activated the module (or it is already
    inactive) there is no write and no event, and ``None`` tells the router
    to answer the same 200 with ``active=false``. Unknown keys raise
    ``UnknownModuleError`` exactly like :func:`activate`. The row (if any)
    keeps ``activated_by_user_id`` as a memory of who turned it on; only the
    audit trail records who turned it off.
    """
    manifest = catalog.get(key)
    row = await _load_activation(session, caller.tenant_id, key)
    if row is None or not row.active:
        return None
    row.active = False
    await session.flush()
    await _persist_state_event(session, caller, "module.deactivated", manifest)
    await session.commit()
    return row
