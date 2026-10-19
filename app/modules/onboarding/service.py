"""Per-user onboarding engine: walk a module's manifest into a connection.

The admin activates a module for the tenant (T1); each user then configures
their own connection by walking the manifest's ``onboarding`` steps (this
module). The engine is declarative: it interprets ``StepSpec`` records, never
executes module code except through the tester registry.

State lifecycle and reads
-------------------------
``not_started`` is *virtual*: :func:`get_state` returns it without writing a
row (GET never mutates), and the row is created lazily on the first
successful :func:`submit_step`. A module with **zero** onboarding steps has
nothing to configure, so reads answer a virtual ``completed`` state (no row,
no connection — there are no answers and no secrets to store).

Strict answer validation (``extra="forbid"`` spirit)
-----------------------------------------------------
Answers are validated against the *current* step only: unknown/extra keys
are rejected, required fields are enforced, and values are bounded by the
declared widget type (``url`` must be http(s), ``number`` must be numeric
and not a bool, ``select`` must sit in the declared ``options`` when they
are declared, ``provider``-named fields must be lowercase identifiers).
``scope_select`` steps accept exactly one ``scopes`` key — a non-empty list
of strings, each inside the manifest's declared ``permissions`` (the
allowlist). ``test_connection`` steps take no answers. Validation errors
raise :class:`OnboardingAnswersError` (router: 422) naming field *keys*,
never submitted values.

Secrets
-------
Fields marked ``secret`` — and every ``password`` widget, secret by type
regardless of the flag — never enter ``state.data``. Their values are
accumulated **outside the answers JSON**, encrypted at rest in
``secrets_encrypted`` under the same ``Settings.credentials_key`` that
protects connection credentials (Fernet, same :mod:`app.core.crypto` as T3).
Plaintext therefore exists only transiently inside one request; the stored
token is cleared when the last step hands the secrets to the connections
service and on restart. A missing or unusable key fails closed
(:class:`OnboardingEncryptionError`) *before* any write — no key, no secret
at rest. (Strict per-request in-memory accumulation was rejected because it
cannot survive the resume-across-requests flows this engine is built for and
would be process-local mutable state.)

Dynamic ``scope_select`` options
--------------------------------
By default a ``scope_select`` step is static: its options are the
manifest's declared ``permissions``. A module whose scope choice lives in
provider data (e.g. the user's Gmail labels) registers a *scope options
provider* (:mod:`app.modules.onboarding.options`, same explicit-registry
style as the tester registry). When one is registered for the module, the
engine invokes it — with the caller's principal, the request session, the
credentials key, and the settings — every time it renders or validates the
step, and the returned options replace the static allowlist (``exclusive``
additionally enforces exactly one choice). The provider may also name its
values (``labels``, e.g. a Gmail label id → the label's name): the view
carries those display labels beside the step (``option_labels``) while
answers keep being validated and persisted as the values. A provider
failure fails closed on both paths, never as a 500: the rendered step keeps
answering 200 with an empty option list plus one fixed actionable
``help_text``, and a submit
is refused with :class:`ScopeOptionsUnavailableError` (router: 409, the
same fixed message). No provider registered = zero behavior change.

Provider rule
-------------
The manifest does not declare providers yet, so at completion time the
provider key is: the non-secret answer to a field literally named
``provider`` when one was given (validated as a lowercase identifier at
submit time), otherwise the ``module_key`` itself.

Step semantics
--------------
- ``form`` / ``scope_select``: validated answers merge into the state and
  the flow advances to the next declared step;
- ``test_connection``: the module's registered tester
  (:mod:`app.modules.onboarding.testers`) receives the connection context;
  no registered tester fails closed (router: 501), ``False`` refuses (409),
  a raise becomes 502 — refusals leave the state untouched;
- ``redirect``: POST-submitting it keeps raising
  :class:`RedirectStepError` (router: 501 naming the provider OAuth
  callback path) for every client — the ONLY completion path is
  :func:`complete_redirect_step`, called by the provider callback (the
  Google flow of :mod:`app.modules.oauth`) after it stored the
  connection. The engine never invents a fake OAuth flow.

Stored-connection bridge (``redirect`` / ``test_connection``)
-------------------------------------------------------------
An OAuth callback writes the tokens to the user's ``UserConnection`` row
*before* the flow advances, so the engine bridges that stored row back into
the step machinery (:func:`_connection_secrets`): when a ``redirect`` or
``test_connection`` step runs, the module's newest stored connection —
decrypted in memory for this call only — merges *under* the accumulated
state secrets for the tester's ``credentials`` context and for the final
credentials payload. State secrets win per key; the connection's provider is
used when the flow never answered a ``provider`` field (an OAuth flow stores
none — the callback knows the provider). Result: a ``test_connection`` step
can verify the OAuth tokens, and completing it never clobbers them with an
empty payload. Steps that only touch user answers (``form`` /
``scope_select``) never load the stored connection.

Completion
----------
When the last step completes: the provider rule resolves, the accumulated
secrets become the credentials payload (JSON, same canonical serialization
as the connections API), and
:func:`~app.modules.connections.service.create_or_update` writes the user's
connection as ``connected`` with the non-secret answers as display
``metadata`` — *before* the state row is marked ``completed``, so a failed
connection write leaves the flow resumable on its last step. The
connections service commits the shared unit of work (same session), matching
the per-function commit semantics of the catalog and connections services.

Audit (:func:`stage_event <app.modules.audit.service.stage_event>`,
staged on the caller's transaction and committed atomically with it):
``onboarding.started`` (first submit),
``onboarding.step_completed`` (every successful step), ``onboarding.completed``
(final step), ``onboarding.restarted`` (:func:`restart`). Payloads are
exactly ``{module_key, step_key, status}`` — never answers, never secrets.
"""

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import (
    CredentialsKeyFormatError,
    DecryptionError,
    decrypt_credentials,
    encrypt_credentials,
)
from app.modules.audit.service import stage_event
from app.modules.catalog.manifest import FieldSpec, ModuleManifest, StepSpec
from app.modules.catalog.registry import ModuleCatalog
from app.modules.catalog.service import active_module_keys
from app.modules.connections.models import UserConnection
from app.modules.connections.service import create_or_update, revoke
from app.modules.identity.deps import CurrentUser
from app.modules.onboarding.exceptions import (
    ConnectionTestError,
    ConnectionTestFailedError,
    ModuleInactiveError,
    OnboardingAnswersError,
    OnboardingCompletedError,
    OnboardingEncryptionError,
    RedirectStepError,
    ScopeOptionsUnavailableError,
    TesterNotRegisteredError,
    WrongStepError,
)
from app.modules.onboarding.models import OnboardingState, OnboardingStatus
from app.modules.onboarding.options import (
    ScopeOptions,
    ScopeOptionsContext,
    ScopeOptionsError,
    scope_options_providers,
)
from app.modules.onboarding.testers import ConnectionContext, ConnectionTester

logger = logging.getLogger(__name__)

_RESOURCE = "onboarding"

#: Lowercase identifier-ish values (``caldav`` / ``google_mail``); same shape
#: as the connections contract's provider rule.
_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")

#: Minimal shape check for ``email`` widgets (no extra dependency for a hint).
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+\Z")

_FIXED_ENCRYPTION = "credentials encryption is not configured"
_FIXED_DECRYPTION = "stored onboarding answers could not be decrypted"

#: One fixed actionable message for a failed scope options provider — the
#: rendered step's ``help_text`` and the submit refusal detail share it, so
#: a module failure never surfaces provider internals (fail closed).
_SCOPE_OPTIONS_UNAVAILABLE = (
    "scope options could not be loaded; reconnect the account and try again"
)

#: One canonical serialized form for the credentials payload (write-only),
#: mirroring the connections API's dict handling.
_JSON_OPTIONS = {"ensure_ascii": False, "separators": (",", ":"), "sort_keys": True}

#: Step types whose machinery may need the module's stored connection row:
#: ``test_connection`` verifies it, and a ``redirect`` step's completion
#: (final or not) must never clobber the tokens the OAuth callback stored.
_CONNECTION_AWARE_STEPS = frozenset({"redirect", "test_connection"})


@dataclass(frozen=True)
class OnboardingViewState:
    """Public snapshot of one user's onboarding for one module.

    ``step`` is the CURRENT step's declarative definition (already shaped for
    a generic renderer — ``scope_select`` carries its synthesized ``scopes``
    field), or ``None`` when the flow is completed. ``data`` holds the
    non-secret answers only; secrets have no place in this view at all.
    ``option_labels`` maps a field key to the display labels of its option
    values (only a dynamic ``scope_select`` carries any today); an option
    without an entry is displayed as its value.
    """

    module_key: str
    status: OnboardingStatus
    current_step: str | None
    step: StepSpec | None
    data: dict[str, Any]
    created_at: datetime | None
    updated_at: datetime | None
    option_labels: Mapping[str, Mapping[str, str]] = dataclass_field(default_factory=dict)


def _is_secret(field: FieldSpec) -> bool:
    """``secret`` flag or a ``password`` widget — password is secret by type."""
    return field.secret or field.type == "password"


def _step_definition(
    manifest: ModuleManifest,
    step: StepSpec,
    *,
    scope_options: ScopeOptions | None = None,
    scope_options_error: str | None = None,
) -> StepSpec:
    """Declarative view of ``step`` for the generic renderer.

    ``scope_select`` synthesizes its single ``scopes`` field from the
    manifest's declared ``permissions`` — the allowlist the engine validates
    against — so the future UI needs no per-module code. When the module
    registered a scope options provider, the fetched ``scope_options``
    replace that static allowlist, and a provider failure
    (``scope_options_error``) becomes one fixed actionable ``help_text`` over
    an *empty* option list (the step renders, but nothing can be chosen —
    never the stale static allowlist). Form fields whose *effective* secrecy
    differs from the declared flag (``password`` widgets are secret by
    type) are re-flagged, so the renderer always sees the rule the engine
    enforces.
    """
    if step.type == "scope_select":
        if scope_options_error is not None:
            options: tuple[str, ...] = ()
        elif scope_options is not None:
            options = scope_options.options
        else:
            options = manifest.permissions
        return StepSpec(
            key=step.key,
            type=step.type,
            title=step.title,
            description=step.description,
            fields=(
                FieldSpec(
                    key="scopes",
                    label="Scopes",
                    type="select",
                    required=True,
                    options=options,
                    help_text=scope_options_error or "",
                ),
            ),
        )
    if step.type == "form":
        fields = tuple(
            replace(field, secret=True) if _is_secret(field) and not field.secret else field
            for field in step.fields
        )
        if fields != step.fields:
            return replace(step, fields=fields)
    return step


def _option_labels(
    step: StepSpec,
    *,
    scope_options: ScopeOptions | None = None,
    scope_options_error: str | None = None,
) -> dict[str, dict[str, str]]:
    """Display labels of ``step``'s option values, keyed by field key.

    Only a dynamic ``scope_select`` has them: the provider's ``labels``
    restricted to the values it actually offers, non-empty strings only
    (a stale or blank label never reaches the renderer). Static options and
    a failed provider carry none — the renderer then shows the values.
    """
    if step.type != "scope_select" or scope_options is None or scope_options_error is not None:
        return {}
    labels: dict[str, str] = {}
    for value in scope_options.options:
        label = scope_options.labels.get(value)
        if isinstance(label, str) and label:
            labels[value] = label
    return {"scopes": labels} if labels else {}


def _find_step(manifest: ModuleManifest, step_key: str) -> StepSpec:
    """Return the declared step ``step_key`` (the flow's keys are unique)."""
    for step in manifest.onboarding:
        if step.key == step_key:
            return step
    raise WrongStepError("step is not the current onboarding step")


def _check_value(field: FieldSpec, value: Any) -> Any:
    """Bound one form answer by its widget type; returns the JSON-ready value.

    Raises:
        OnboardingAnswersError: the value violates the declared type or
            constraint (the message names the key, never the value).
    """
    widget = field.type
    if widget == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise OnboardingAnswersError(f"answer {field.key!r} must be a number")
        return value
    if not isinstance(value, str) or not value.strip():
        raise OnboardingAnswersError(f"answer {field.key!r} must be a non-empty string")
    if widget == "email" and not _EMAIL.fullmatch(value):
        raise OnboardingAnswersError(f"answer {field.key!r} must be an email address")
    if widget == "url" and not value.startswith(("http://", "https://")):
        raise OnboardingAnswersError(f"answer {field.key!r} must be an http(s) URL")
    if widget == "select" and field.options and value not in field.options:
        raise OnboardingAnswersError(f"answer {field.key!r} must be one of the declared options")
    if field.key == "provider" and not _PROVIDER.fullmatch(value):
        raise OnboardingAnswersError(
            f"answer {field.key!r} must be a lowercase identifier like 'caldav'"
        )
    return value


def _validate_form(
    step: StepSpec, answers: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate ``form`` answers; returns ``(non_secret, secrets)`` dicts."""
    known = {field.key for field in step.fields}
    unknown = sorted(set(answers) - known)
    if unknown:
        raise OnboardingAnswersError(f"unknown answer field(s): {', '.join(unknown)}")
    non_secret: dict[str, Any] = {}
    secrets: dict[str, str] = {}
    for field in step.fields:
        value = answers.get(field.key)
        if value is None:
            if field.required:
                raise OnboardingAnswersError(f"missing required answer {field.key!r}")
            continue
        checked = _check_value(field, value)
        if _is_secret(field):
            secrets[field.key] = str(checked)
        else:
            non_secret[field.key] = checked
    return non_secret, secrets


def _validate_scopes(
    manifest: ModuleManifest, answers: Mapping[str, Any], scope_options: ScopeOptions | None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate ``scope_select`` answers: one non-empty allowlisted list.

    The allowlist is the module's registered dynamic ``scope_options`` when
    one exists (the provider's values, plus its ``exclusive`` single-choice
    rule), otherwise the manifest's declared ``permissions``.
    """
    unknown = sorted(set(answers) - {"scopes"})
    if unknown:
        raise OnboardingAnswersError(f"unknown answer field(s): {', '.join(unknown)}")
    scopes = answers.get("scopes")
    if not isinstance(scopes, list) or not scopes:
        raise OnboardingAnswersError("answer 'scopes' must be a non-empty list")
    if any(not isinstance(item, str) for item in scopes):
        raise OnboardingAnswersError("answer 'scopes' must list strings")
    if scope_options is None:
        allowed = set(manifest.permissions)
    else:
        if scope_options.exclusive and len(scopes) != 1:
            raise OnboardingAnswersError("answer 'scopes' accepts exactly one choice")
        allowed = set(scope_options.options)
    for scope in scopes:
        if scope not in allowed:
            raise OnboardingAnswersError(f"scope {scope!r} is not declared by the module")
    return {"scopes": list(scopes)}, {}


def _validate_answers(
    manifest: ModuleManifest,
    step: StepSpec,
    answers: Mapping[str, Any],
    scope_options: ScopeOptions | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Strictly validate one step's answers; returns ``(non_secret, secrets)``."""
    if step.type == "form":
        return _validate_form(step, answers)
    if step.type == "scope_select":
        return _validate_scopes(manifest, answers, scope_options)
    if step.type == "test_connection":
        if answers:
            raise OnboardingAnswersError("test_connection step takes no answers")
        return {}, {}
    raise OnboardingAnswersError(f"step type {step.type!r} takes no answers")


def _resolve_provider(module_key: str, data: Mapping[str, Any]) -> str:
    """Provider rule: a ``provider`` answer, else the module key itself."""
    provider = data.get("provider")
    if isinstance(provider, str) and _PROVIDER.fullmatch(provider):
        return provider
    return module_key


def _seal_secrets(credentials_key: str | None, secrets: dict[str, str]) -> str | None:
    """Encrypt the accumulated secrets; fail closed without a usable key."""
    if not secrets:
        return None
    if credentials_key is None:
        raise OnboardingEncryptionError(_FIXED_ENCRYPTION)
    try:
        return encrypt_credentials(credentials_key, json.dumps(secrets, **_JSON_OPTIONS))
    except CredentialsKeyFormatError as exc:
        raise OnboardingEncryptionError(_FIXED_ENCRYPTION) from exc


def _open_secrets(credentials_key: str | None, state: OnboardingState | None) -> dict[str, str]:
    """Decrypt the stored secrets of ``state`` (empty when none were stored)."""
    if state is None or state.secrets_encrypted is None:
        return {}
    if credentials_key is None:
        raise OnboardingEncryptionError(_FIXED_ENCRYPTION)
    try:
        return json.loads(decrypt_credentials(credentials_key, state.secrets_encrypted))
    except (CredentialsKeyFormatError, DecryptionError, json.JSONDecodeError) as exc:
        raise OnboardingEncryptionError(_FIXED_DECRYPTION) from exc


async def _connection_secrets(
    session: AsyncSession,
    *,
    tenant_id: int,
    user_id: int,
    module_key: str,
    credentials_key: str | None,
) -> tuple[dict[str, Any], str | None]:
    """Decrypt the module's stored connection credentials for the tester bridge.

    Loads the NEWEST ``UserConnection`` row of one (tenant, user, module) —
    the MVP stores one provider row per module — and returns
    ``(secrets, provider)``. ``secrets`` is the decrypted JSON blob an OAuth
    callback stored (``access_token``, ``refresh_token``, ``expires_at``,
    ``scopes`` …) or ``{}`` when no row or no blob exists; ``provider`` is
    that row's provider (``None`` when no row exists). A missing or unusable
    ``credentials_key`` over an existing blob fails closed
    (:class:`OnboardingEncryptionError`) — same rule as the state secrets.
    Plaintext exists only in memory for the current call; nothing here is
    logged, audited, or persisted.
    """
    result = await session.execute(
        select(UserConnection)
        .where(
            UserConnection.tenant_id == tenant_id,
            UserConnection.user_id == user_id,
            UserConnection.module_key == module_key,
        )
        .order_by(UserConnection.created_at.desc(), UserConnection.id.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None or row.credentials_encrypted is None:
        return {}, row.provider if row is not None else None
    if credentials_key is None:
        raise OnboardingEncryptionError(_FIXED_ENCRYPTION)
    try:
        decrypted = json.loads(decrypt_credentials(credentials_key, row.credentials_encrypted))
    except (CredentialsKeyFormatError, DecryptionError, json.JSONDecodeError) as exc:
        raise OnboardingEncryptionError(_FIXED_DECRYPTION) from exc
    return decrypted, row.provider


async def _require_active(
    session: AsyncSession, catalog: ModuleCatalog, caller: CurrentUser, module_key: str
) -> ModuleManifest:
    """Manifest lookup plus the tenant's activation gate (before any state)."""
    manifest = catalog.get(module_key)
    active = await active_module_keys(session, caller.tenant_id)
    if module_key not in active:
        raise ModuleInactiveError(f"module {module_key!r} is not active for this tenant")
    return manifest


async def _load(
    session: AsyncSession, *, tenant_id: int, user_id: int, module_key: str
) -> OnboardingState | None:
    """Return the caller's row for one module, or ``None`` when never started."""
    result = await session.execute(
        select(OnboardingState).where(
            OnboardingState.tenant_id == tenant_id,
            OnboardingState.user_id == user_id,
            OnboardingState.module_key == module_key,
        )
    )
    return result.scalar_one_or_none()


async def _scope_options_for(
    session: AsyncSession,
    *,
    caller: CurrentUser,
    module_key: str,
    credentials_key: str | None,
    settings: Settings | None,
) -> tuple[ScopeOptions | None, str | None]:
    """Fetch the module's dynamic scope options; ``(None, None)`` = static.

    Returns ``(options, None)`` on success, ``(None, message)`` when a
    registered provider failed (:class:`ScopeOptionsError` *or* any
    unexpected raise — both fail closed with the same fixed actionable
    message, never provider internals and never a 500), and
    ``(None, None)`` when the module registered no provider, keeping the
    static ``manifest.permissions`` contract untouched.
    """
    provider = scope_options_providers.get(module_key)
    if provider is None:
        return None, None
    context = ScopeOptionsContext(
        caller=caller,
        session=session,
        module_key=module_key,
        credentials_key=credentials_key,
        settings=settings,
    )
    try:
        return await provider(context), None
    except ScopeOptionsError:
        logger.warning(
            "scope options provider refused for module %s",
            module_key,
            extra={"module_key": module_key},
        )
        return None, _SCOPE_OPTIONS_UNAVAILABLE
    except Exception:
        logger.warning(
            "scope options provider failed for module %s",
            module_key,
            exc_info=True,
            extra={"module_key": module_key},
        )
        return None, _SCOPE_OPTIONS_UNAVAILABLE


def _pending_step(manifest: ModuleManifest, state: OnboardingState | None) -> StepSpec | None:
    """The step :func:`_view` would render, or ``None`` when nothing renders."""
    if not manifest.onboarding:
        return None
    if state is None:
        return manifest.onboarding[0]
    if state.status is OnboardingStatus.COMPLETED or state.current_step is None:
        return None
    return _find_step(manifest, state.current_step)


async def _render(
    session: AsyncSession,
    *,
    manifest: ModuleManifest,
    state: OnboardingState | None,
    caller: CurrentUser,
    credentials_key: str | None,
    settings: Settings | None,
) -> OnboardingViewState:
    """:func:`_view` plus the module's dynamic scope options, when needed.

    The options are fetched only when the step about to render is a
    ``scope_select`` of a module that registered a provider — every other
    step, and every module without a provider, keeps the synchronous static
    path with zero extra work.
    """
    scope_options: ScopeOptions | None = None
    scope_options_error: str | None = None
    step = _pending_step(manifest, state)
    if step is not None and step.type == "scope_select":
        scope_options, scope_options_error = await _scope_options_for(
            session,
            caller=caller,
            module_key=manifest.key,
            credentials_key=credentials_key,
            settings=settings,
        )
    return _view(
        manifest, state, scope_options=scope_options, scope_options_error=scope_options_error
    )


def _view(
    manifest: ModuleManifest,
    state: OnboardingState | None,
    *,
    scope_options: ScopeOptions | None = None,
    scope_options_error: str | None = None,
) -> OnboardingViewState:
    """Build the public snapshot from a manifest and (maybe) a stored row."""
    if not manifest.onboarding:
        # Nothing declared → nothing to configure → vacuously completed.
        return OnboardingViewState(
            module_key=manifest.key,
            status=OnboardingStatus.COMPLETED,
            current_step=None,
            step=None,
            data={},
            created_at=None,
            updated_at=None,
        )
    if state is None:
        first = manifest.onboarding[0]
        return OnboardingViewState(
            module_key=manifest.key,
            status=OnboardingStatus.NOT_STARTED,
            current_step=first.key,
            step=_step_definition(
                manifest,
                first,
                scope_options=scope_options,
                scope_options_error=scope_options_error,
            ),
            data={},
            created_at=None,
            updated_at=None,
            option_labels=_option_labels(
                first, scope_options=scope_options, scope_options_error=scope_options_error
            ),
        )
    if state.status is OnboardingStatus.COMPLETED or state.current_step is None:
        return OnboardingViewState(
            module_key=manifest.key,
            status=state.status,
            current_step=None,
            step=None,
            data=dict(state.data or {}),
            created_at=state.created_at,
            updated_at=state.updated_at,
        )
    step = _find_step(manifest, state.current_step)
    return OnboardingViewState(
        module_key=manifest.key,
        status=state.status,
        current_step=state.current_step,
        step=_step_definition(
            manifest, step, scope_options=scope_options, scope_options_error=scope_options_error
        ),
        data=dict(state.data or {}),
        created_at=state.created_at,
        updated_at=state.updated_at,
        option_labels=_option_labels(
            step, scope_options=scope_options, scope_options_error=scope_options_error
        ),
    )


async def _persist_state_event(
    session: AsyncSession,
    caller: CurrentUser,
    action: str,
    *,
    module_key: str,
    step_key: str,
    status: OnboardingStatus,
) -> None:
    """Stage one onboarding event on the caller's transaction (never answers
    or secrets).

    Staged, not independently committed: the caller commits right after, so
    action and audit share SQLite's single writer atomically instead of
    deadlocking on a second connection.
    """
    stage_event(
        session,
        tenant_id=caller.tenant_id,
        action=action,
        actor_user_id=caller.id,
        resource_type=_RESOURCE,
        resource_id=module_key,
        payload={
            "module_key": module_key,
            "step_key": step_key,
            "status": status.value,
        },
    )


async def _refresh_after_flush(session: AsyncSession, state: OnboardingState) -> None:
    """Reload ``state`` so its server-managed ``updated_at`` is hydrated again.

    ``TimestampMixin.updated_at`` has a server ``onupdate``: flushing an
    UPDATE leaves that column expired, and a later *synchronous* attribute
    access (the router serializing ``OnboardingStateResponse``) would attempt
    IO outside the greenlet — ``MissingGreenlet``.
    """
    await session.refresh(state)


async def get_state(
    session: AsyncSession,
    *,
    catalog: ModuleCatalog,
    caller: CurrentUser,
    module_key: str,
    credentials_key: str | None = None,
    settings: Settings | None = None,
) -> OnboardingViewState:
    """Return the caller's own onboarding state plus the current step definition.

    A read never writes state: an untouched flow answers a virtual
    ``not_started`` (row created only on the first successful submit), and a
    module with no declared steps answers a virtual ``completed``. Unknown
    modules raise ``UnknownModuleError`` (catalog, router: 404); modules the
    tenant has not active raise :class:`ModuleInactiveError` (router: 409).
    ``credentials_key``/``settings`` are forwarded to the module's scope
    options provider when the rendered step is a dynamic ``scope_select``.
    """
    manifest = await _require_active(session, catalog, caller, module_key)
    state = await _load(
        session, tenant_id=caller.tenant_id, user_id=caller.id, module_key=module_key
    )
    return await _render(
        session,
        manifest=manifest,
        state=state,
        caller=caller,
        credentials_key=credentials_key,
        settings=settings,
    )


async def submit_step(
    session: AsyncSession,
    *,
    catalog: ModuleCatalog,
    credentials_key: str | None,
    testers: Mapping[str, ConnectionTester],
    caller: CurrentUser,
    module_key: str,
    step_key: str,
    answers: Mapping[str, Any],
    settings: Settings | None = None,
) -> OnboardingViewState:
    """Validate and apply one step of the caller's own onboarding flow.

    ``step_key`` must be the flow's current step (router: 409 otherwise), and
    ``answers`` are validated strictly against that step's declaration
    (router: 422 on violation). A dynamic ``scope_select`` step re-fetches
    the module's scope options for the validation; when that fetch fails the
    submit is refused with :class:`ScopeOptionsUnavailableError` (router:
    409) — no options, no choice. Refusals — wrong step, invalid answers, a
    redirect placeholder, a missing/failing tester — leave the stored state
    completely untouched. On the last step the accumulated secrets become
    the connection's encrypted credentials and the state turns ``completed``.
    """
    manifest = await _require_active(session, catalog, caller, module_key)
    state = await _load(
        session, tenant_id=caller.tenant_id, user_id=caller.id, module_key=module_key
    )
    if state is not None and state.status is OnboardingStatus.COMPLETED:
        raise OnboardingCompletedError("onboarding is already completed")
    if not manifest.onboarding:
        raise WrongStepError("step is not the current onboarding step")
    current_key = state.current_step if state is not None else manifest.onboarding[0].key
    if step_key != current_key:
        raise WrongStepError("step is not the current onboarding step")
    step = _find_step(manifest, step_key)
    if step.type == "redirect":
        # Generic contract: only the provider OAuth callback completes it
        # (:func:`complete_redirect_step`); no fake OAuth flow is invented here.
        raise RedirectStepError(
            "redirect steps complete at the provider OAuth callback "
            "(/api/v1/onboarding/{module_key}/callback); not supported in this release"
        )
    scope_options: ScopeOptions | None = None
    if step.type == "scope_select":
        scope_options, scope_options_error = await _scope_options_for(
            session,
            caller=caller,
            module_key=module_key,
            credentials_key=credentials_key,
            settings=settings,
        )
        if scope_options_error is not None:
            raise ScopeOptionsUnavailableError(scope_options_error)
    non_secret, secrets = _validate_answers(manifest, step, answers, scope_options)
    return await _advance(
        session,
        manifest=manifest,
        catalog=catalog,
        credentials_key=credentials_key,
        testers=testers,
        caller=caller,
        module_key=module_key,
        step=step,
        non_secret=non_secret,
        secrets=secrets,
        state=state,
        settings=settings,
    )


async def _advance(
    session: AsyncSession,
    *,
    manifest: ModuleManifest,
    catalog: ModuleCatalog,
    credentials_key: str | None,
    testers: Mapping[str, ConnectionTester],
    caller: CurrentUser,
    module_key: str,
    step: StepSpec,
    non_secret: Mapping[str, Any],
    secrets: Mapping[str, str],
    state: OnboardingState | None,
    settings: Settings | None = None,
) -> OnboardingViewState:
    """Apply one already-validated step: advance, audit, commit, return the view.

    The shared transition machinery of :func:`submit_step` and
    :func:`complete_redirect_step`: the ``non_secret`` answers and ``secrets``
    merge over the stored state, the flow advances to the next declared step
    (or completes, writing the connection BEFORE marking the state
    ``completed``), and the same ``onboarding.started`` /
    ``onboarding.step_completed`` / ``onboarding.completed`` events are
    audited. ``redirect`` and ``test_connection`` steps additionally bridge
    the module's stored connection (:func:`_connection_secrets`) under the
    state secrets for the tester context and the final credentials payload —
    so completing an OAuth flow never clobbers the tokens the callback
    stored. Refusals (a failing tester, a failed connection write) leave the
    stored state untouched.
    """
    merged_data = {**(state.data if state is not None else {}), **non_secret}
    state_secrets = {**_open_secrets(credentials_key, state), **secrets}
    provider = _resolve_provider(module_key, merged_data)
    index = next(i for i, item in enumerate(manifest.onboarding) if item.key == step.key)
    is_final = index == len(manifest.onboarding) - 1
    connection_secrets: dict[str, Any] = {}
    if step.type in _CONNECTION_AWARE_STEPS:
        connection_secrets, connection_provider = await _connection_secrets(
            session,
            tenant_id=caller.tenant_id,
            user_id=caller.id,
            module_key=module_key,
            credentials_key=credentials_key,
        )
        # An OAuth-stored connection knows its provider; the manifest's
        # provider rule only wins when the flow actually answered the field.
        if connection_provider is not None and "provider" not in merged_data:
            provider = connection_provider
    if step.type == "test_connection":
        context = ConnectionContext(
            tenant_id=caller.tenant_id,
            user_id=caller.id,
            module_key=module_key,
            provider=provider,
            credentials={**connection_secrets, **state_secrets},
            data=merged_data,
        )
        await _run_tester(testers, context)
    if is_final:
        final_secrets = {**connection_secrets, **state_secrets}
        if credentials_key is None and final_secrets:
            # Fail closed before the connection write (no key, no secret at rest).
            raise OnboardingEncryptionError(_FIXED_ENCRYPTION)
        credentials = json.dumps(final_secrets, **_JSON_OPTIONS)
        # Connection first: a failed write leaves the flow parked on this step.
        await create_or_update(
            session,
            catalog=catalog,
            credentials_key=credentials_key,
            caller=caller,
            module_key=module_key,
            provider=provider,
            credentials=credentials,
            metadata=dict(merged_data),
        )
        next_status = OnboardingStatus.COMPLETED
        next_step: str | None = None
        secrets_token = None
    else:
        next_status = OnboardingStatus.IN_PROGRESS
        next_step = manifest.onboarding[index + 1].key
        # Only the state's own secrets travel in the token: stored connection
        # secrets stay in the connection row (single source of truth).
        secrets_token = _seal_secrets(credentials_key, state_secrets)
    created = state is None
    if state is None:
        state = OnboardingState(
            tenant_id=caller.tenant_id,
            user_id=caller.id,
            module_key=module_key,
            status=next_status,
            current_step=next_step,
            data=merged_data,
            secrets_encrypted=secrets_token,
        )
        session.add(state)
    else:
        state.status = next_status
        state.current_step = next_step
        state.data = merged_data
        state.secrets_encrypted = secrets_token
    await session.flush()
    await _refresh_after_flush(session, state)
    if created:
        await _persist_state_event(
            session,
            caller,
            "onboarding.started",
            module_key=module_key,
            step_key=next_step if next_step is not None else step.key,
            status=next_status,
        )
    await _persist_state_event(
        session,
        caller,
        "onboarding.step_completed",
        module_key=module_key,
        step_key=step.key,
        status=next_status,
    )
    if is_final:
        await _persist_state_event(
            session,
            caller,
            "onboarding.completed",
            module_key=module_key,
            step_key=step.key,
            status=next_status,
        )
    await session.commit()
    return await _render(
        session,
        manifest=manifest,
        state=state,
        caller=caller,
        credentials_key=credentials_key,
        settings=settings,
    )


async def complete_redirect_step(
    session: AsyncSession,
    *,
    catalog: ModuleCatalog,
    credentials_key: str | None,
    caller: CurrentUser,
    module_key: str,
    settings: Settings | None = None,
) -> OnboardingViewState:
    """Complete the module's current ``redirect`` step — the OAuth callback path.

    The provider callback (today: the Google flow in :mod:`app.modules.oauth`)
    calls this AFTER storing the connection: the module must be active for the
    tenant (router-less seam: :class:`ModuleInactiveError` /
    ``UnknownModuleError`` propagate), and the caller's flow must be parked
    on — or, virtually, about to start at — the manifest's ``redirect`` step
    (``WrongStepError`` otherwise, ``OnboardingCompletedError`` on a
    completed flow). The step advances through the same machinery as a
    submitted form step (:func:`_advance`), auditing the same
    ``onboarding.step_completed`` event; when the redirect step is the LAST
    declared step the flow completes, and the final credentials payload
    keeps the stored connection secrets (the OAuth tokens) under any state
    secrets.

    POST-submitting a ``redirect`` step keeps raising
    :class:`RedirectStepError` (router: 501) for every client — this function
    is the only completion path. The OAuth callback treats every error here
    as best-effort: the connection IS stored, so it logs and lands the
    browser on the wizard anyway.
    """
    manifest = await _require_active(session, catalog, caller, module_key)
    state = await _load(
        session, tenant_id=caller.tenant_id, user_id=caller.id, module_key=module_key
    )
    if state is not None and state.status is OnboardingStatus.COMPLETED:
        raise OnboardingCompletedError("onboarding is already completed")
    if not manifest.onboarding:
        raise WrongStepError("step is not the current onboarding step")
    current_key = state.current_step if state is not None else manifest.onboarding[0].key
    step = _find_step(manifest, current_key)
    if step.type != "redirect":
        raise WrongStepError("step is not the current onboarding step")
    return await _advance(
        session,
        manifest=manifest,
        catalog=catalog,
        credentials_key=credentials_key,
        testers={},
        caller=caller,
        module_key=module_key,
        step=step,
        non_secret={},
        secrets={},
        state=state,
        settings=settings,
    )


async def _run_tester(testers: Mapping[str, ConnectionTester], context: ConnectionContext) -> None:
    """Run the module's tester; fail closed when absent, map failures to errors."""
    tester = testers.get(context.module_key)
    if tester is None:
        raise TesterNotRegisteredError(
            f"no connection tester registered for module {context.module_key!r}"
        )
    try:
        passed = await tester(context)
    except Exception as exc:
        raise ConnectionTestError("connection test could not complete") from exc
    if not passed:
        raise ConnectionTestFailedError("connection test failed")


async def _revoke_connections(
    session: AsyncSession, *, caller: CurrentUser, module_key: str
) -> None:
    """Destroy the stored connections of one module for the caller (D7).

    Every row of the caller's (tenant, user, module) goes through
    :func:`connections.service.revoke`: status ``revoked`` AND
    ``credentials_encrypted`` destroyed — the secret is unrecoverable, only a
    fresh OAuth flow can reconnect. Status-revoked (not row removal) keeps
    the ``UNIQUE (tenant, user, module, provider)`` natural key stable, so
    the next completion re-activates the same row through create-or-update
    and the audit trail keeps one stable connection id
    (``connection.revoked``, payload ``{module_key, provider, status}`` —
    never secrets). Safe when no connection exists: nothing to revoke.
    """
    rows = (
        (
            await session.execute(
                select(UserConnection).where(
                    UserConnection.tenant_id == caller.tenant_id,
                    UserConnection.user_id == caller.id,
                    UserConnection.module_key == module_key,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        await revoke(session, caller=caller, connection_id=row.id)


async def restart(
    session: AsyncSession,
    *,
    catalog: ModuleCatalog,
    caller: CurrentUser,
    module_key: str,
    credentials_key: str | None = None,
    settings: Settings | None = None,
) -> OnboardingViewState:
    """Reset the caller's completed onboarding so they can reconfigure (D7).

    A completed flow returns to ``in_progress`` parked on the first declared
    step: ``data`` and the secret token are cleared and the event
    ``onboarding.restarted`` is audited. When the manifest declares a
    ``redirect`` step (an OAuth flow), the stored connection is destroyed as
    well (:func:`_revoke_connections`): restart means reconnecting from
    scratch, never reusing tokens the user just discarded. A flow that is
    not completed (or a module with no steps) is returned unchanged —
    nothing to restart, nothing revoked. ``credentials_key``/``settings``
    forward to a dynamic ``scope_select`` provider, as in :func:`get_state`.
    """
    manifest = await _require_active(session, catalog, caller, module_key)
    state = await _load(
        session, tenant_id=caller.tenant_id, user_id=caller.id, module_key=module_key
    )
    if state is None or state.status is not OnboardingStatus.COMPLETED:
        return await _render(
            session,
            manifest=manifest,
            state=state,
            caller=caller,
            credentials_key=credentials_key,
            settings=settings,
        )
    if not manifest.onboarding:
        # Degenerate: the row outlived a flow that no longer declares steps.
        return await _render(
            session,
            manifest=manifest,
            state=state,
            caller=caller,
            credentials_key=credentials_key,
            settings=settings,
        )
    first = manifest.onboarding[0]
    state.status = OnboardingStatus.IN_PROGRESS
    state.current_step = first.key
    state.data = {}
    state.secrets_encrypted = None
    await session.flush()
    await _refresh_after_flush(session, state)
    await _persist_state_event(
        session,
        caller,
        "onboarding.restarted",
        module_key=module_key,
        step_key=first.key,
        status=OnboardingStatus.IN_PROGRESS,
    )
    await session.commit()
    if any(step.type == "redirect" for step in manifest.onboarding):
        # After the state commit: revoke() commits its own unit of work.
        await _revoke_connections(session, caller=caller, module_key=module_key)
    return await _render(
        session,
        manifest=manifest,
        state=state,
        caller=caller,
        credentials_key=credentials_key,
        settings=settings,
    )


__all__ = [
    "OnboardingViewState",
    "complete_redirect_step",
    "get_state",
    "restart",
    "submit_step",
]
