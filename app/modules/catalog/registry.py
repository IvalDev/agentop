"""Module catalog: validated manifests and the process-wide default catalog.

A *module* is a named, versioned bundle of capability an admin switches on
for one tenant (settings panel), as opposed to a :class:`~app.modules.tools.registry.ToolSpec`,
which is a single callable tool. The catalog is the code-side allowlist of
modules: the API can only show or activate what is registered here, so
shipping a new module is a reviewed code change, never a data entry.

Registration invariants are enforced by :meth:`ModuleCatalog.register`:

- ``key`` is a lowercase identifier (``calendar`` / ``google_mail``),
  unique in the catalog;
- ``name`` and ``description`` are non-blank (both are shown to admins);
- every onboarding step has a non-empty, unique ``key`` and a ``type`` of
  ``form``, ``redirect``, ``scope_select``, or ``test_connection``.

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


class ModuleCatalog:
    """Key-keyed collection of validated module manifests."""

    def __init__(self) -> None:
        self._manifests: dict[str, ModuleManifest] = {}

    def register(self, manifest: ModuleManifest) -> None:
        """Validate ``manifest`` against every invariant, then store it.

        Raises:
            ModuleRegistrationError: duplicate or invalid ``key``, blank
                ``name`` or ``description``, or an onboarding step with an
                empty key, a duplicate key, or an unsupported type.
        """
        if not isinstance(manifest.key, str) or not _KEY_PATTERN.fullmatch(manifest.key):
            raise ModuleRegistrationError(
                f"invalid module key {manifest.key!r}: expected lowercase identifier "
                "like 'calendar'"
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


#: Process-wide catalog populated by module packages at import (none in T1).
default_catalog = ModuleCatalog()


def register_default(manifest: ModuleManifest) -> ModuleManifest:
    """Register ``manifest`` on :data:`default_catalog` and return it.

    Explicit import-time registration (Python's module cache makes it
    idempotent across ``create_app`` calls in tests).
    """
    default_catalog.register(manifest)
    return manifest
