"""OAuth connection endpoints: start (authenticated) + Google callback (browser).

HTTP shape (one place):

========================================================= ==================== ==================
route                                                     meaning              access
========================================================= ==================== ==================
``GET /api/v1/oauth/{module_key}/authorize``              302 to Google's      any authenticated
                                                          consent screen
``POST /api/v1/oauth/{module_key}/authorize``             200 JSON {"url"}     any authenticated
                                                          for the SPA (a
                                                          browser navigation
                                                          cannot carry the
                                                          bearer header; GET
                                                          stays for
                                                          navigations that
                                                          can)
``GET /api/v1/oauth/google/callback``                     302 to the module's  browser (no
                                                          onboarding wizard    bearer — the
                                                          after storing        pending state
                                                          tokens + completing  identifies the
                                                          the redirect step    flow)
========================================================= ==================== ==================

Start order (checked first to last): settings → module → redirect step →
scopes. Missing Google credentials answer 501 ``Google OAuth is not
configured; set AA_GOOGLE_CLIENT_ID and AA_GOOGLE_CLIENT_SECRET`` even for an
unknown module; an unknown module is 404 ``module not found``; a manifest
without a ``redirect`` step or without permissions is 400. Both start routes
share that order and the same pending-state/PKCE build: the browser then
either follows GET's 302 or, for a top-level navigation that cannot carry the
``Authorization`` header (the SPA's onboarding wizard), POSTs for the consent
URL first and navigates itself.

Callback rules: the identity comes from the one-shot pending record (this is
unauthenticated browser traffic — no bearer header, no cookies), consumed
exactly once. Unknown / expired / reused states share one fixed 400
``invalid or expired OAuth state``; the provider's ``error`` parameter is 400;
a missing ``code`` is 400; a refused exchange is 400 ``token exchange
failed``; an unreachable token endpoint is 502; a missing/malformed
encryption key fails closed with 500 ``credentials encryption is not
configured`` and stores nothing.

Success: the tokens are written through
:func:`create_or_update <app.modules.connections.service.create_or_update>`
(write-only, encrypted at rest, status ``connected``, provider ``google``) —
which audits ``connection.created`` / ``connection.updated`` with
``{module_key, provider, status}``, the same payload as every other write path
(no extra audit event here — the onboarding engine emits its own
``onboarding.step_completed`` when it advances the redirect step). The
stored blob carries ``{access_token, refresh_token?, expires_at, scopes}`` —
never the client secret, which stays in settings. The callback then advances
the module's onboarding flow through
:func:`complete_redirect_step
<app.modules.onboarding.service.complete_redirect_step>` (T2: the ONLY path
that completes a ``redirect`` step — POST submit stays 501) and answers a
302 to the D8 generic wizard route
``/configuracion/herramientas/{module_key}/onboarding``. That completion is
BEST-EFFORT: the connection is already stored, so a missing/inactive/
completed onboarding flow (or any engine error) is logged server-side and
the browser still lands on the wizard — which shows the real stored state
on the next visit. The web app does not serve the wizard route until T4;
until then the landing is a harmless 404 (a redirect flow is a browser
navigation, not an API resource, so no contract schema changes).

The tester bridge (``test_connection`` after this callback) lives in the
onboarding engine: it decrypts this stored row into
``ConnectionContext.credentials`` (``credentials["access_token"]`` per
:mod:`app.modules.calendar.testers`).
"""

import json
import logging
import time
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import get_session
from app.core.deps import get_settings
from app.modules.catalog.exceptions import UnknownModuleError
from app.modules.catalog.registry import ModuleCatalog
from app.modules.catalog.router import get_module_catalog
from app.modules.connections.exceptions import CredentialsEncryptionError
from app.modules.connections.service import create_or_update
from app.modules.identity.deps import CurrentUser, get_current_user
from app.modules.oauth import service
from app.modules.onboarding.service import complete_redirect_step

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/oauth", tags=["oauth"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ModuleCatalogDep = Annotated[ModuleCatalog, Depends(get_module_catalog)]

#: Base path of the web app's tools section; a successful callback lands on
#: ``{base}/{module_key}/onboarding`` — the D8 generic wizard route. The web
#: app does not serve it until T4; until then the landing is a harmless 404.
TOOLS_BASE_PATH = "/configuracion/herramientas"


def onboarding_landing_path(module_key: str) -> str:
    """Where the browser lands after a successful connection (D8 wizard)."""
    return f"{TOOLS_BASE_PATH}/{module_key}/onboarding"


_NOT_CONFIGURED = (
    "Google OAuth is not configured; set AA_GOOGLE_CLIENT_ID and AA_GOOGLE_CLIENT_SECRET"
)
_BAD_STATE = "invalid or expired OAuth state"
_MODULE_NOT_FOUND = "module not found"
_NO_REDIRECT_STEP = "module has no redirect step"
_NO_SCOPES = "module declares no scopes"
_PROVIDER_ERROR = "authorization failed"
_NO_CODE = "missing authorization code"
_EXCHANGE_REFUSED = "token exchange failed"
_UNREACHABLE = "Google is unreachable"
_NO_KEY = "credentials encryption is not configured"


def _google_credentials(settings: Settings) -> tuple[str, str]:
    """The configured client id and secret, or one 501 (shared by both routes)."""
    client_id = settings.google_client_id.get_secret_value() if settings.google_client_id else ""
    client_secret = (
        settings.google_client_secret.get_secret_value() if settings.google_client_secret else ""
    )
    if not client_id or not client_secret:
        raise HTTPException(status_code=501, detail=_NOT_CONFIGURED)
    return client_id, client_secret


def _credentials_key(settings: Settings) -> str | None:
    """The raw Fernet key, or ``None`` when unset/blank (fail closed downstream)."""
    if settings.credentials_key is None:
        return None
    return settings.credentials_key.get_secret_value() or None


@router.get("/google/callback", name="google_oauth_callback")
async def google_oauth_callback(
    request: Request,
    session: SessionDep,
    catalog: ModuleCatalogDep,
    settings: SettingsDep,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Finish the Google flow: validate state once, exchange, store, redirect.

    Registered before ``/{module_key}/authorize`` so the literal path can never
    be swallowed by the parameter route (same rule as the connections router).
    """
    pending = service.consume_pending(state)
    if pending is None:
        raise HTTPException(status_code=400, detail=_BAD_STATE)
    if error is not None:
        raise HTTPException(status_code=400, detail=_PROVIDER_ERROR)
    if not code:
        raise HTTPException(status_code=400, detail=_NO_CODE)
    client_id, client_secret = _google_credentials(settings)
    try:
        tokens = await service.exchange_code(
            code=code,
            redirect_uri=str(request.url_for("google_oauth_callback")),
            client_id=client_id,
            client_secret=client_secret,
            code_verifier=pending.pkce_verifier,
        )
    except service.TokenExchangeError as exc:
        raise HTTPException(status_code=400, detail=_EXCHANGE_REFUSED) from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=_UNREACHABLE) from exc
    credentials: dict[str, object] = {
        "access_token": tokens.access_token,
        "scopes": list(tokens.scopes),
    }
    if tokens.refresh_token is not None:
        credentials["refresh_token"] = tokens.refresh_token
    if tokens.expires_in is not None:
        credentials["expires_at"] = int(time.time()) + tokens.expires_in
    blob = json.dumps(credentials, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    try:
        await create_or_update(
            session,
            catalog=catalog,
            credentials_key=_credentials_key(settings),
            caller=pending.user,
            module_key=pending.module_key,
            provider="google",
            credentials=blob,
            metadata={"provider": "google"},
        )
    except CredentialsEncryptionError as exc:
        raise HTTPException(status_code=500, detail=_NO_KEY) from exc
    except UnknownModuleError as exc:
        raise HTTPException(status_code=404, detail=_MODULE_NOT_FOUND) from exc
    try:
        await complete_redirect_step(
            session,
            catalog=catalog,
            credentials_key=_credentials_key(settings),
            caller=pending.user,
            module_key=pending.module_key,
        )
    except Exception:
        # Best effort (T2): the connection IS stored above; a missing,
        # inactive, or already-completed onboarding flow — or any engine
        # error — must not break the browser landing. The wizard shows the
        # real stored state on the next visit; the failure is logged.
        logger.warning(
            "oauth callback could not complete the onboarding redirect step",
            exc_info=True,
            extra={"module_key": pending.module_key},
        )
    return RedirectResponse(url=onboarding_landing_path(pending.module_key), status_code=302)


@router.get("/{module_key}/authorize")
async def start_module_authorization(
    module_key: str,
    request: Request,
    user: CurrentUserDep,
    catalog: ModuleCatalogDep,
    settings: SettingsDep,
) -> RedirectResponse:
    """Send an authenticated user to Google's consent screen (302, PKCE)."""
    return RedirectResponse(
        url=_authorization_url(
            module_key, request=request, user=user, catalog=catalog, settings=settings
        ),
        status_code=302,
    )


@router.post("/{module_key}/authorize")
async def start_module_authorization_json(
    module_key: str,
    request: Request,
    user: CurrentUserDep,
    catalog: ModuleCatalogDep,
    settings: SettingsDep,
) -> dict[str, str]:
    """The same consent URL as JSON (200) for the SPA's fetch-then-navigate.

    A top-level browser navigation cannot carry the ``Authorization`` header,
    so the onboarding wizard fetches the URL here (authenticated) and calls
    ``window.location.assign(url)`` itself.
    """
    url = _authorization_url(
        module_key, request=request, user=user, catalog=catalog, settings=settings
    )
    return {"url": url}


def _authorization_url(
    module_key: str,
    *,
    request: Request,
    user: CurrentUser,
    catalog: ModuleCatalog,
    settings: Settings,
) -> str:
    """Run the start checks in order and build Google's consent URL (shared by GET/POST)."""
    client_id, _client_secret = _google_credentials(settings)
    try:
        manifest = catalog.get(module_key)
    except UnknownModuleError as exc:
        raise HTTPException(status_code=404, detail=_MODULE_NOT_FOUND) from exc
    redirect_step = next((step for step in manifest.onboarding if step.type == "redirect"), None)
    if redirect_step is None:
        raise HTTPException(status_code=400, detail=_NO_REDIRECT_STEP)
    if not manifest.permissions:
        raise HTTPException(status_code=400, detail=_NO_SCOPES)
    state = service.new_state()
    verifier, challenge = service.new_pkce()
    service.store_pending(
        state,
        user=user,
        module_key=manifest.key,
        step_key=redirect_step.key,
        pkce_verifier=verifier,
    )
    # Both credentials were validated above; only the public id reaches the
    # consent URL (never the secret, never the verifier).
    return service.build_authorization_url(
        client_id=client_id,
        redirect_uri=str(request.url_for("google_oauth_callback")),
        scopes=manifest.permissions,
        state=state,
        code_challenge=challenge,
    )
