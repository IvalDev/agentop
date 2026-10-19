"""Widget installer publication API tests.

T7 publishes the Linux bundles built by ``make widget-package`` so the web
console can offer them from a page every role can reach.

HTTP shape under test:

============================================= =============================
route                                         meaning
============================================= =============================
``GET /api/v1/widget/installers``             published bundles + parsed
                                              version/arch/size
``GET /api/v1/widget/installers/{filename}``  the file itself
============================================= =============================

Every route requires an authenticated caller (any role) and only ever serves
files whose names follow the Tauri bundle scheme, so a stray file in the
directory — or a traversal attempt — is a 404, never a read outside it.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app

BASE = "/api/v1/widget/installers"

#: The two bundles a Linux release produces, plus noise that must be ignored.
PUBLISHED = (
    "Asistente_0.1.0_amd64.deb",
    "Asistente_0.1.0_amd64.AppImage",
    "notes.txt",
    "Asistente_0.1.0_amd64.exe",
    "Asistente_beta_amd64.deb",
)


def _build_app(seeded_db, settings: Settings) -> FastAPI:
    """App wired with the seeded database and the given settings."""
    application: FastAPI = create_app()
    application.state.db = seeded_db
    application.state.settings = settings
    return application


def _settings(identity_settings: Settings, directory: Path) -> Settings:
    return identity_settings.model_copy(update={"widget_installers_dir": str(directory)})


@pytest.fixture
def widget_app(seeded_db, identity_settings: Settings, tmp_path: Path) -> FastAPI:
    """App whose installer directory holds exactly the published fixtures."""
    directory = tmp_path / "widget"
    directory.mkdir()
    for name in PUBLISHED:
        (directory / name).write_bytes(b"bundle")
    return _build_app(seeded_db, _settings(identity_settings, directory))


@pytest.fixture
async def widget_client(widget_app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=widget_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


async def _headers(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "tenant_slug": "acme"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_installer_routes_require_authentication(widget_client: AsyncClient) -> None:
    """Anonymous callers get 401 on the list and on a download."""
    assert (await widget_client.get(BASE)).status_code == 401
    assert (await widget_client.get(f"{BASE}/Asistente_0.1.0_amd64.deb")).status_code == 401


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("owner@acme.test", "owner-pass"),
        ("admin@acme.test", "admin-pass"),
        ("viewer@acme.test", "viewer-pass"),
    ],
)
async def test_list_returns_the_published_bundles_for_every_role(
    widget_client: AsyncClient, widget_app: FastAPI, email: str, password: str
) -> None:
    """Every role reads both bundles with their parsed metadata, and only those."""
    headers = await _headers(widget_client, email, password)
    directory = Path(widget_app.state.settings.widget_installers_dir)

    response = await widget_client.get(BASE, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert [installer["filename"] for installer in body["installers"]] == [
        "Asistente_0.1.0_amd64.AppImage",
        "Asistente_0.1.0_amd64.deb",
    ]
    appimage, deb = body["installers"]
    assert appimage == {
        "filename": "Asistente_0.1.0_amd64.AppImage",
        "package_type": "appimage",
        "version": "0.1.0",
        "arch": "amd64",
        "size_bytes": (directory / appimage["filename"]).stat().st_size,
        "download_url": f"{BASE}/Asistente_0.1.0_amd64.AppImage",
    }
    assert deb["package_type"] == "deb"
    assert deb["version"] == "0.1.0"
    assert "tenant_id" not in body


async def test_list_is_empty_when_nothing_is_published(
    seeded_db, identity_settings: Settings, tmp_path: Path
) -> None:
    """An empty publication directory answers an empty list, never an error."""
    directory = tmp_path / "empty"
    directory.mkdir()
    application = _build_app(seeded_db, _settings(identity_settings, directory))
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _headers(client, "viewer@acme.test", "viewer-pass")
        response = await client.get(BASE, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"installers": []}


async def test_list_is_empty_when_the_directory_does_not_exist(
    seeded_db, identity_settings: Settings, tmp_path: Path
) -> None:
    """A missing publication directory is an empty catalog, not a 500."""
    application = _build_app(seeded_db, _settings(identity_settings, tmp_path / "absent"))
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _headers(client, "owner@acme.test", "owner-pass")
        response = await client.get(BASE, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"installers": []}


async def test_download_serves_the_published_file(widget_client: AsyncClient) -> None:
    """The download answers the exact bytes with an attachment disposition."""
    headers = await _headers(widget_client, "admin@acme.test", "admin-pass")

    response = await widget_client.get(f"{BASE}/Asistente_0.1.0_amd64.deb", headers=headers)

    assert response.status_code == 200
    assert response.content == b"bundle"
    assert response.headers["content-disposition"] == (
        'attachment; filename="Asistente_0.1.0_amd64.deb"'
    )
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "name",
    [
        "notes.txt",
        "Asistente_9.9.9_amd64.deb",
        "Asistente_0.1.0_amd64.exe",
        "Asistente_beta_amd64.deb",
        "..%2Fsecret",
        "Asistente_0.1.0_amd64.deb%2F..%2Fnotes.txt",
    ],
)
async def test_download_refuses_anything_not_published(
    widget_client: AsyncClient, name: str
) -> None:
    """Only a name that matches the bundle scheme AND exists is served."""
    headers = await _headers(widget_client, "owner@acme.test", "owner-pass")

    response = await widget_client.get(f"{BASE}/{name}", headers=headers)

    assert response.status_code == 404


def test_published_names_are_parsed_from_the_tauri_bundle_scheme(tmp_path: Path) -> None:
    """The parser reads version, arch and package type, and rejects anything else."""
    from app.modules.widget.service import parse_installer_name

    assert parse_installer_name("Asistente_1.2.3_amd64.deb") == (
        "deb",
        "1.2.3",
        "amd64",
    )
    assert parse_installer_name("Asistente_0.1.0_aarch64.AppImage") == (
        "appimage",
        "0.1.0",
        "aarch64",
    )
    assert parse_installer_name("Asistente_1.2_amd64.deb") == ("deb", "1.2", "amd64")
    for rejected in (
        "notes.txt",
        "Asistente_beta_amd64.deb",
        "Asistente_1.2.3_amd64.exe",
        "Asistente_1.2.3_.deb",
        "asistente_1.2.3_amd64.deb",
    ):
        assert parse_installer_name(rejected) is None
