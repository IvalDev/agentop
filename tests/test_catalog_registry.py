"""Module catalog registry tests: registration invariants and lookup errors.

Covers the T1 catalog contract: valid manifests register and round-trip;
duplicate keys, invalid keys, blank name/description, and malformed
onboarding steps (empty or duplicated step keys, unknown step types) raise
``ModuleRegistrationError``; unknown lookups raise ``UnknownModuleError``.
The process-wide default catalog ships empty: a production module may only
appear after someone deliberately extends the allowlist, so adding one later
is a reviewed decision instead of a silent side effect.
"""

from dataclasses import FrozenInstanceError

import pytest

from app.modules.catalog.exceptions import ModuleRegistrationError, UnknownModuleError
from app.modules.catalog.manifest import FieldSpec, ModuleManifest, StepSpec
from app.modules.catalog.registry import ModuleCatalog, default_catalog, register_default

#: Production modules registered on the process-wide catalog; none yet (T1).
MODULE_CATALOG_ALLOWLIST: list[str] = []
INVALID_KEYS = ["Demo Module", "google-mail", "2fast", "", "UPPER_snake", " spaced"]


def build_manifest(
    key: str = "demo_module",
    *,
    name: str = "Demo Module",
    description: str = "registry test module",
    version: str = "1.0.0",
    permissions: tuple[str, ...] = (),
    tools: tuple[str, ...] = (),
    onboarding: tuple[StepSpec, ...] = (),
) -> ModuleManifest:
    """Build a ModuleManifest with test defaults; individual fields can be overridden."""
    return ModuleManifest(
        key=key,
        name=name,
        description=description,
        version=version,
        permissions=permissions,
        tools=tools,
        onboarding=onboarding,
    )


def test_register_and_get_round_trip() -> None:
    """A valid manifest registers and is retrievable by key."""
    catalog = ModuleCatalog()
    manifest = build_manifest()

    catalog.register(manifest)

    assert catalog.get("demo_module") is manifest


def test_keys_and_list_manifests_reflect_registrations() -> None:
    """``keys`` and ``list_manifests`` return every manifest in registration order."""
    catalog = ModuleCatalog()
    first = build_manifest("alpha_module")
    second = build_manifest("beta_module")

    catalog.register(first)
    catalog.register(second)

    assert catalog.keys() == ["alpha_module", "beta_module"]
    assert catalog.list_manifests() == [first, second]


def test_manifest_is_frozen() -> None:
    """Manifests are data-only records: registration-time state never mutates."""
    manifest = build_manifest()

    with pytest.raises(FrozenInstanceError):
        manifest.name = "Renamed"


def test_duplicate_key_raises() -> None:
    """Registering the same key twice is a registration error."""
    catalog = ModuleCatalog()
    catalog.register(build_manifest("demo_module"))

    with pytest.raises(ModuleRegistrationError, match="duplicate"):
        catalog.register(build_manifest("demo_module"))


@pytest.mark.parametrize("bad_key", INVALID_KEYS)
def test_invalid_key_raises(bad_key: str) -> None:
    """Keys must be lowercase identifier-ish strings (``calendar``)."""
    catalog = ModuleCatalog()

    with pytest.raises(ModuleRegistrationError, match="invalid module key"):
        catalog.register(build_manifest(key=bad_key))


@pytest.mark.parametrize("bad_name", ["", "   "])
def test_blank_name_raises(bad_name: str) -> None:
    """A manifest with no display name cannot be shown in the catalog."""
    catalog = ModuleCatalog()

    with pytest.raises(ModuleRegistrationError, match="non-empty name"):
        catalog.register(build_manifest(name=bad_name))


@pytest.mark.parametrize("bad_description", ["", "   "])
def test_blank_description_raises(bad_description: str) -> None:
    """A manifest without a description gives admins no reason to enable it."""
    catalog = ModuleCatalog()

    with pytest.raises(ModuleRegistrationError, match="non-empty description"):
        catalog.register(build_manifest(description=bad_description))


def test_onboarding_steps_round_trip() -> None:
    """Declarative steps and their fields are stored exactly as declared."""
    catalog = ModuleCatalog()
    manifest = build_manifest(
        onboarding=(
            StepSpec(
                key="connect",
                type="form",
                title="Connect the provider",
                description="Enter the account credentials",
                fields=(
                    FieldSpec(key="endpoint", label="Server URL", type="url"),
                    FieldSpec(key="password", label="Password", type="password", secret=True),
                ),
            ),
            StepSpec(key="verify", type="test_connection", title="Verify the connection"),
        )
    )

    catalog.register(manifest)

    stored = catalog.get("demo_module")
    assert stored.onboarding == manifest.onboarding
    assert stored.onboarding[0].fields[1].secret is True


def test_unknown_step_type_raises() -> None:
    """Only the four declarative step types the T4 engine will support exist."""
    catalog = ModuleCatalog()
    manifest = build_manifest(onboarding=(StepSpec(key="connect", type="oauth"),))

    with pytest.raises(ModuleRegistrationError, match="invalid type"):
        catalog.register(manifest)


def test_duplicate_step_key_raises() -> None:
    """Step keys are the engine's addressing scheme: they must be unique."""
    catalog = ModuleCatalog()
    manifest = build_manifest(
        onboarding=(
            StepSpec(key="connect", type="form"),
            StepSpec(key="connect", type="test_connection"),
        )
    )

    with pytest.raises(ModuleRegistrationError, match="duplicate onboarding step"):
        catalog.register(manifest)


def test_empty_step_key_raises() -> None:
    """A step without a key can never be resumed or addressed."""
    catalog = ModuleCatalog()
    manifest = build_manifest(onboarding=(StepSpec(key="", type="form"),))

    with pytest.raises(ModuleRegistrationError, match="empty key"):
        catalog.register(manifest)


def test_get_unknown_key_raises_not_found() -> None:
    """Looking up an unregistered key raises ``UnknownModuleError``."""
    catalog = ModuleCatalog()

    with pytest.raises(UnknownModuleError, match="unknown module"):
        catalog.get("ghost_module")


def test_default_catalog_exposes_only_allowlisted_modules() -> None:
    """No production module may appear without being added to the allowlist."""
    assert default_catalog.keys() == MODULE_CATALOG_ALLOWLIST


def test_register_default_registers_on_the_process_catalog() -> None:
    """``register_default`` stores on the shared catalog and returns the manifest."""
    manifest = build_manifest("default_probe")
    try:
        assert register_default(manifest) is manifest
        assert default_catalog.get("default_probe") is manifest
    finally:
        # Roll back so the process-wide allowlist test stays order-independent.
        default_catalog._manifests.pop("default_probe", None)
