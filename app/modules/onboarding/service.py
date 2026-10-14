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
- ``redirect``: declarative placeholder in this foundations slice — the
  engine refuses to auto-complete it (router: 501 naming the future OAuth
  callback path); no fake OAuth flow is invented here.

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

Audit (:func:`persist_event <app.modules.audit.service.persist_event>`,
own transaction): ``onboarding.started`` (first submit),
``onboarding.step_completed`` (every successful step), ``onboarding.completed``
(final step), ``onboarding.restarted`` (:func:`restart`). Payloads are
exactly ``{module_key, step_key, status}`` — never answers, never secrets.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import (
    CredentialsKeyFormatError,
    DecryptionError,
    decrypt_credentials,
    encrypt_credentials,
)
from app.modules.audit.service import persist_event
from app.modules.catalog.manifest import FieldSpec, ModuleManifest, StepSpec
from app.modules.catalog.registry import ModuleCatalog
from app.modules.catalog.service import active_module_keys
from app.modules.connections.service import create_or_update
from app.modules.identity.deps import CurrentUser
from app.modules.onboarding.exceptions import (
    ConnectionTestError,
    ConnectionTestFailedError,
    ModuleInactiveError,
    OnboardingAnswersError,
    OnboardingCompletedError,
    OnboardingEncryptionError,
    RedirectStepError,
    TesterNotRegisteredError,
    WrongStepError,
)
from app.modules.onboarding.models import OnboardingState, OnboardingStatus
from app.modules.onboarding.testers import ConnectionContext, ConnectionTester

_RESOURCE = "onboarding"

#: Lowercase identifier-ish values (``caldav`` / ``google_mail``); same shape
#: as the connections contract's provider rule.
_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")

#: Minimal shape check for ``email`` widgets (no extra dependency for a hint).
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+\Z")

_FIXED_ENCRYPTION = "credentials encryption is not configured"
_FIXED_DECRYPTION = "stored onboarding answers could not be decrypted"

#: One canonical serialized form for the credentials payload (write-only),
#: mirroring the connections API's dict handling.
_JSON_OPTIONS = {"ensure_ascii": False, "separators": (",", ":"), "sort_keys": True}


@dataclass(frozen=True)
class OnboardingViewState:
    """Public snapshot of one user's onboarding for one module.

    ``step`` is the CURRENT step's declarative definition (already shaped for
    a generic renderer — ``scope_select`` carries its synthesized ``scopes``
    field), or ``None`` when the flow is completed. ``data`` holds the
    non-secret answers only; secrets have no place in this view at all.
    """

    module_key: str
    status: OnboardingStatus
    current_step: str | None
    step: StepSpec | None
    data: dict[str, Any]
    created_at: datetime | None
    updated_at: datetime | None


def _is_secret(field: FieldSpec) -> bool:
    """``secret`` flag or a ``password`` widget — password is secret by type."""
    return field.secret or field.type == "password"


def _step_definition(manifest: ModuleManifest, step: StepSpec) -> StepSpec:
    """Declarative view of ``step`` for the generic renderer.

    ``scope_select`` synthesizes its single ``scopes`` field from the
    manifest's declared ``permissions`` — the allowlist the engine validates
    against — so the future UI needs no per-module code. Form fields whose
    *effective* secrecy differs from the declared flag (``password`` widgets
    are secret by type) are re-flagged, so the renderer always sees the rule
    the engine enforces.
    """
    if step.type == "scope_select":
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
                    options=manifest.permissions,
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
    manifest: ModuleManifest, answers: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate ``scope_select`` answers: one non-empty allowlisted list."""
    unknown = sorted(set(answers) - {"scopes"})
    if unknown:
        raise OnboardingAnswersError(f"unknown answer field(s): {', '.join(unknown)}")
    scopes = answers.get("scopes")
    if not isinstance(scopes, list) or not scopes:
        raise OnboardingAnswersError("answer 'scopes' must be a non-empty list")
    if any(not isinstance(item, str) for item in scopes):
        raise OnboardingAnswersError("answer 'scopes' must list strings")
    for scope in scopes:
        if scope not in manifest.permissions:
            raise OnboardingAnswersError(f"scope {scope!r} is not declared by the module")
    return {"scopes": list(scopes)}, {}


def _validate_answers(
    manifest: ModuleManifest, step: StepSpec, answers: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Strictly validate one step's answers; returns ``(non_secret, secrets)``."""
    if step.type == "form":
        return _validate_form(step, answers)
    if step.type == "scope_select":
        return _validate_scopes(manifest, answers)
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


def _view(manifest: ModuleManifest, state: OnboardingState | None) -> OnboardingViewState:
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
            step=_step_definition(manifest, first),
            data={},
            created_at=None,
            updated_at=None,
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
        step=_step_definition(manifest, step),
        data=dict(state.data or {}),
        created_at=state.created_at,
        updated_at=state.updated_at,
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
    """Write one onboarding event (own transaction; never answers or secrets)."""
    await persist_event(
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
) -> OnboardingViewState:
    """Return the caller's own onboarding state plus the current step definition.

    A read never writes: an untouched flow answers a virtual ``not_started``
    (row created only on the first successful submit), and a module with no
    declared steps answers a virtual ``completed``. Unknown modules raise
    ``UnknownModuleError`` (catalog, router: 404); modules the tenant has not
    active raise :class:`ModuleInactiveError` (router: 409).
    """
    manifest = await _require_active(session, catalog, caller, module_key)
    state = await _load(
        session, tenant_id=caller.tenant_id, user_id=caller.id, module_key=module_key
    )
    return _view(manifest, state)


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
) -> OnboardingViewState:
    """Validate and apply one step of the caller's own onboarding flow.

    ``step_key`` must be the flow's current step (router: 409 otherwise), and
    ``answers`` are validated strictly against that step's declaration
    (router: 422 on violation). Refusals — wrong step, invalid answers, a
    redirect placeholder, a missing/failing tester — leave the stored state
    completely untouched. On the last step the accumulated secrets become the
    connection's encrypted credentials and the state turns ``completed``.
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
        # Declarative placeholder: never invent a fake OAuth flow (see module docs).
        raise RedirectStepError(
            "redirect steps complete at the provider OAuth callback "
            "(/api/v1/onboarding/{module_key}/callback); not supported in this release"
        )
    non_secret, secrets = _validate_answers(manifest, step, answers)
    merged_data = {**(state.data if state is not None else {}), **non_secret}
    merged_secrets = {**_open_secrets(credentials_key, state), **secrets}
    provider = _resolve_provider(module_key, merged_data)
    if step.type == "test_connection":
        context = ConnectionContext(
            tenant_id=caller.tenant_id,
            user_id=caller.id,
            module_key=module_key,
            provider=provider,
            credentials=merged_secrets,
            data=merged_data,
        )
        await _run_tester(testers, context)
    index = next(i for i, item in enumerate(manifest.onboarding) if item.key == step_key)
    is_final = index == len(manifest.onboarding) - 1
    if is_final:
        if credentials_key is None and merged_secrets:
            # Fail closed before the connection write (no key, no secret at rest).
            raise OnboardingEncryptionError(_FIXED_ENCRYPTION)
        credentials = json.dumps(merged_secrets, **_JSON_OPTIONS)
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
        secrets_token = _seal_secrets(credentials_key, merged_secrets)
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
            step_key=next_step if next_step is not None else step_key,
            status=next_status,
        )
    await _persist_state_event(
        session,
        caller,
        "onboarding.step_completed",
        module_key=module_key,
        step_key=step_key,
        status=next_status,
    )
    if is_final:
        await _persist_state_event(
            session,
            caller,
            "onboarding.completed",
            module_key=module_key,
            step_key=step_key,
            status=next_status,
        )
    await session.commit()
    return _view(manifest, state)


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


async def restart(
    session: AsyncSession,
    *,
    catalog: ModuleCatalog,
    caller: CurrentUser,
    module_key: str,
) -> OnboardingViewState:
    """Reset the caller's completed onboarding so they can reconfigure.

    A completed flow returns to ``in_progress`` parked on the first declared
    step: ``data`` and the secret token are cleared and the event
    ``onboarding.restarted`` is audited. A flow that is not completed (or a
    module with no steps) is returned unchanged — nothing to restart. The
    existing connection row survives until the user completes again, where
    create-or-update replaces its credentials.
    """
    manifest = await _require_active(session, catalog, caller, module_key)
    state = await _load(
        session, tenant_id=caller.tenant_id, user_id=caller.id, module_key=module_key
    )
    if state is None or state.status is not OnboardingStatus.COMPLETED:
        return _view(manifest, state)
    if not manifest.onboarding:
        # Degenerate: the row outlived a flow that no longer declares steps.
        return _view(manifest, state)
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
    return _view(manifest, state)


__all__ = [
    "OnboardingViewState",
    "get_state",
    "restart",
    "submit_step",
]
