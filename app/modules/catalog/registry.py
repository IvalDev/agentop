"""Module catalog: validated manifests and the process-wide default catalog.

A *module* is a named, versioned bundle of capability an admin switches on
for one tenant (settings panel), as opposed to a :class:`~app.modules.tools.registry.ToolSpec`,
which is a single callable tool. The catalog is the code-side allowlist of
modules: the API can only show or activate what is registered here, so
shipping a new module is a reviewed code change, never a data entry.

Registration invariants are enforced by :meth:`ModuleCatalog.register`:

- ``key`` is a lowercase identifier (``calendar`` / ``google_mail``),
  unique in the catalog, and not reserved (``knowledge`` names the agent's
  built-in engine);
- ``name`` and ``description`` are non-blank (both are shown to admins);
- every onboarding step has a non-empty, unique ``key`` and a ``type`` of
  ``form``, ``redirect``, ``scope_select``, or ``test_connection``;
- an ``agent`` profile, when present, has a non-blank
  ``routing_description`` of at most 300 characters and non-blank
  ``instructions`` of at most 2000 characters.

Invalid manifests raise :class:`~app.modules.catalog.exceptions.ModuleRegistrationError`;
lookups raise :class:`~app.modules.catalog.exceptions.UnknownModuleError`.
Single-process MVP: no thread-safety guarantees — same contract as the tool
registry.
"""

import re

from app.modules.catalog.exceptions import ModuleRegistrationError, UnknownModuleError
from app.modules.catalog.manifest import STEP_TYPES, ModuleManifest

# Lowercase identifier-ish keys such as ``calendar`` / ``google_mail``.
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: Keys no module may take: ``knowledge`` is the agent's built-in engine.
RESERVED_MODULE_KEYS: frozenset[str] = frozenset({"knowledge"})
#: Limits of an :class:`~app.modules.catalog.manifest.AgentProfile` (they ride in prompts).
ROUTING_DESCRIPTION_MAX_CHARS = 300
AGENT_INSTRUCTIONS_MAX_CHARS = 2000


class ModuleCatalog:
    """Key-keyed collection of validated module manifests."""

    def __init__(self) -> None:
        self._manifests: dict[str, ModuleManifest] = {}

    def register(self, manifest: ModuleManifest) -> None:
        """Validate ``manifest`` against every invariant, then store it.

        Raises:
            ModuleRegistrationError: duplicate, invalid, or reserved ``key``,
                blank ``name`` or ``description``, an onboarding step with an
                empty key, a duplicate key, or an unsupported type, or an
                ``agent`` profile with a blank or too long field.
        """
        if not isinstance(manifest.key, str) or not _KEY_PATTERN.fullmatch(manifest.key):
            raise ModuleRegistrationError(
                f"invalid module key {manifest.key!r}: expected lowercase identifier "
                "like 'calendar'"
            )
        if manifest.key in RESERVED_MODULE_KEYS:
            raise ModuleRegistrationError(
                f"module key {manifest.key!r} is reserved for the agent's built-in engine"
            )
        if manifest.key in self._manifests:
            raise ModuleRegistrationError(f"duplicate module key: {manifest.key!r}")
        if not isinstance(manifest.name, str) or not manifest.name.strip():
            raise ModuleRegistrationError(f"module {manifest.key!r} needs a non-empty name")
        if not isinstance(manifest.description, str) or not manifest.description.strip():
            raise ModuleRegistrationError(f"module {manifest.key!r} needs a non-empty description")
        seen: set[str] = set()
        for step in manifest.onboarding:
            if not isinstance(step.key, str) or not step.key.strip():
                raise ModuleRegistrationError(
                    f"module {manifest.key!r} has an onboarding step with an empty key"
                )
            if step.key in seen:
                raise ModuleRegistrationError(
                    f"module {manifest.key!r} has duplicate onboarding step key {step.key!r}"
                )
            seen.add(step.key)
            if step.type not in STEP_TYPES:
                raise ModuleRegistrationError(
                    f"module {manifest.key!r} step {step.key!r} has invalid type {step.type!r}: "
                    "expected form, redirect, scope_select, or test_connection"
                )
        if manifest.agent is not None:
            _check_agent_profile(manifest)
        self._manifests[manifest.key] = manifest

    def get(self, key: str) -> ModuleManifest:
        """Return the manifest registered under ``key``.

        Raises:
            UnknownModuleError: no module with that key.
        """
        try:
            return self._manifests[key]
        except KeyError:
            raise UnknownModuleError(f"unknown module: {key!r}") from None

    def list_manifests(self) -> list[ModuleManifest]:
        """Return every registered manifest in registration order."""
        return list(self._manifests.values())

    def keys(self) -> list[str]:
        """Return every registered module key in registration order."""
        return list(self._manifests)


def _check_agent_profile(manifest: ModuleManifest) -> None:
    """Raise ``ModuleRegistrationError`` unless both profile fields are non-blank and short."""
    profile = manifest.agent
    limits = (
        ("routing_description", ROUTING_DESCRIPTION_MAX_CHARS),
        ("instructions", AGENT_INSTRUCTIONS_MAX_CHARS),
    )
    for field, limit in limits:
        value = getattr(profile, field, None)
        if not isinstance(value, str) or not value.strip():
            raise ModuleRegistrationError(
                f"module {manifest.key!r} needs a non-empty agent {field}"
            )
        if len(value) > limit:
            raise ModuleRegistrationError(
                f"module {manifest.key!r} agent {field} exceeds {limit} characters"
            )


#: Process-wide catalog populated by module packages at import (none in T1).
default_catalog = ModuleCatalog()


def register_default(manifest: ModuleManifest) -> ModuleManifest:
    """Register ``manifest`` on :data:`default_catalog` and return it.

    Explicit import-time registration (Python's module cache makes it
    idempotent across ``create_app`` calls in tests).
    """
    default_catalog.register(manifest)
    return manifest
