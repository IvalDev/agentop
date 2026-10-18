"""Per-user onboarding API: own state, submit the current step, restart.

HTTP shape (one place, :data:`_HTTP_STATUS`):

===================================================== ==================== =========
route                                                 meaning              access
===================================================== ==================== =========
``GET /api/v1/onboarding/{module_key}``                own state + current  any
                                                      step definition      authenticated
``POST /api/v1/onboarding/{module_key}/steps/{step}``  submit one step's    any
                                                      answers
``POST /api/v1/onboarding/{module_key}/restart``       reset a completed    any
                                                      flow to step one
===================================================== ==================== =========

Every route is the *caller's own* flow: tenant and user come from the
authenticated principal, never from the path or body, so one user can neither
read nor advance another user's onboarding — foreign-state semantics do not
apply because foreign state is unreachable by construction.

Rules (the service enforces them; this router only translates errors to
HTTP, one place in :data:`_HTTP_STATUS`): an unknown ``module_key`` is 404;
a module not active for the tenant is 409 (a conflict with the tenant's
enabled state, not a permission denial); a step that is not the flow's
current step, a submit to a completed flow, and a failed connection test are
409; invalid answers are 422 (detail names field keys, never values);
``redirect`` placeholders and ``test_connection`` steps without a registered
tester are 501 (capability absent in this foundations slice); a tester that
raises is 502; a failed dynamic scope options provider is 409 with the same
fixed actionable message the rendered step carries; a missing/unusable
encryption key fails closed with 500.
Catalog, settings, and the tester registry come from ``app.state`` (same
pattern as the catalog and connections routers), so test apps swap them per
fixture.

Audit (written in the service): ``onboarding.started`` /
``onboarding.step_completed`` / ``onboarding.completed`` /
``onboarding.restarted`` — payloads are ``{module_key, step_key, status}``
and nothing else: never answers, never secrets.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import get_session
from app.core.deps import get_settings
from app.modules.catalog.exceptions import ModuleCatalogError, UnknownModuleError
from app.modules.catalog.manifest import FieldSpec, StepSpec
from app.modules.catalog.registry import ModuleCatalog
from app.modules.catalog.router import get_module_catalog
from app.modules.identity.deps import CurrentUser, get_current_user
from app.modules.onboarding.exceptions import (
    ConnectionTestError,
    ConnectionTestFailedError,
    ModuleInactiveError,
    OnboardingAnswersError,
    OnboardingCompletedError,
    OnboardingEncryptionError,
    OnboardingError,
    RedirectStepError,
    ScopeOptionsUnavailableError,
    TesterNotRegisteredError,
    WrongStepError,
)
from app.modules.onboarding.service import (
    OnboardingViewState,
    get_state,
    restart,
    submit_step,
)
from app.modules.onboarding.testers import ConnectionTester
from packages.contracts.schemas import (
    OnboardingFieldResponse,
    OnboardingStateResponse,
    OnboardingStepResponse,
    OnboardingStepSubmitRequest,
)

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ModuleCatalogDep = Annotated[ModuleCatalog, Depends(get_module_catalog)]


def get_connection_testers(request: Request) -> dict[str, ConnectionTester]:
    """Return the tester registry attached to ``app.state.connection_testers``."""
    return request.app.state.connection_testers


TestersDep = Annotated[dict[str, ConnectionTester], Depends(get_connection_testers)]

_HTTP_STATUS: dict[type[Exception], tuple[int, str]] = {
    UnknownModuleError: (404, "module not found"),
    ModuleInactiveError: (409, "module is not active for this tenant"),
    WrongStepError: (409, "step is not the current onboarding step"),
    OnboardingCompletedError: (409, "onboarding is already completed"),
    OnboardingAnswersError: (422, "invalid step answers"),
    RedirectStepError: (
        501,
        "redirect steps complete at the provider OAuth callback "
        "(/api/v1/onboarding/{module_key}/callback); not supported in this release",
    ),
    TesterNotRegisteredError: (501, "no connection tester registered for this module"),
    ConnectionTestFailedError: (409, "connection test failed"),
    ConnectionTestError: (502, "connection test could not complete"),
    OnboardingEncryptionError: (500, "credentials encryption is not configured"),
    ScopeOptionsUnavailableError: (409, "scope options could not be loaded"),
}


def _http_error(exc: Exception) -> HTTPException:
    """Translate a service error; unmapped errors are server bugs (500).

    Answer-validation errors keep the service's message as ``detail``: it
    names the offending field *keys* and constraints (never submitted values,
    which may be secrets), which the future renderer turns into field errors.
    A failed scope options provider keeps its fixed actionable message too
    (the same one the rendered step carries — never provider internals).
    """
    status_code, detail = _HTTP_STATUS.get(type(exc), (500, "internal error"))
    if isinstance(exc, OnboardingAnswersError | ScopeOptionsUnavailableError):
        detail = str(exc)
    return HTTPException(status_code=status_code, detail=detail)


def _field_response(field: FieldSpec) -> OnboardingFieldResponse:
    """Public view of one field (already synthesized for the renderer)."""
    return OnboardingFieldResponse(
        key=field.key,
        label=field.label,
        type=field.type,
        required=field.required,
        secret=field.secret,
        help_text=field.help_text,
        options=list(field.options),
    )


def _step_response(step: StepSpec | None) -> OnboardingStepResponse | None:
    """Public view of the current step's declarative definition (``None`` when done)."""
    if step is None:
        return None
    return OnboardingStepResponse(
        key=step.key,
        type=step.type,
        title=step.title,
        description=step.description,
        fields=[_field_response(field) for field in step.fields],
    )


def _state_response(view: OnboardingViewState) -> OnboardingStateResponse:
    """Public view of the state: non-secret data only, by contract shape."""
    return OnboardingStateResponse(
        module_key=view.module_key,
        status=view.status.value,
        current_step=view.current_step,
        step=_step_response(view.step),
        data=dict(view.data),
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _credentials_key(settings: Settings) -> str | None:
    """The raw Fernet key, or ``None`` when unset/blank (fail closed downstream)."""
    if settings.credentials_key is None:
        return None
    return settings.credentials_key.get_secret_value() or None


@router.get("/{module_key}", response_model=OnboardingStateResponse)
async def get_onboarding_state(
    module_key: str,
    session: SessionDep,
    user: CurrentUserDep,
    catalog: ModuleCatalogDep,
    settings: SettingsDep,
) -> OnboardingStateResponse:
    """The caller's own onboarding state and the current step to render.

    ``settings`` (and its credentials key) reach a registered scope options
    provider when the rendered step is a dynamic ``scope_select``.
    """
    try:
        view = await get_state(
            session,
            catalog=catalog,
            caller=user,
            module_key=module_key,
            credentials_key=_credentials_key(settings),
            settings=settings,
        )
    except (ModuleCatalogError, OnboardingError) as exc:
        raise _http_error(exc) from exc
    return _state_response(view)


@router.post("/{module_key}/steps/{step_key}", response_model=OnboardingStateResponse)
async def submit_onboarding_step(
    module_key: str,
    step_key: str,
    payload: OnboardingStepSubmitRequest,
    session: SessionDep,
    user: CurrentUserDep,
    catalog: ModuleCatalogDep,
    settings: SettingsDep,
    testers: TestersDep,
) -> OnboardingStateResponse:
    """Submit the current step's answers; the body is ``extra="forbid"``."""
    try:
        view = await submit_step(
            session,
            catalog=catalog,
            credentials_key=_credentials_key(settings),
            testers=testers,
            caller=user,
            module_key=module_key,
            step_key=step_key,
            answers=payload.answers,
            settings=settings,
        )
    except (ModuleCatalogError, OnboardingError) as exc:
        raise _http_error(exc) from exc
    return _state_response(view)


@router.post("/{module_key}/restart", response_model=OnboardingStateResponse)
async def restart_onboarding(
    module_key: str,
    session: SessionDep,
    user: CurrentUserDep,
    catalog: ModuleCatalogDep,
    settings: SettingsDep,
) -> OnboardingStateResponse:
    """Reset the caller's completed flow to its first step (no-op otherwise)."""
    try:
        view = await restart(
            session,
            catalog=catalog,
            caller=user,
            module_key=module_key,
            credentials_key=_credentials_key(settings),
            settings=settings,
        )
    except (ModuleCatalogError, OnboardingError) as exc:
        raise _http_error(exc) from exc
    return _state_response(view)
