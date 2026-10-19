"""Publication of the desktop widget installers (T7).

``make widget-package`` builds the Tauri bundles into
``desktop/src-tauri/target/release/bundle`` and ``make widget-publish`` copies
them into a directory this service reads (``AA_WIDGET_INSTALLERS_DIR``,
``./var/widget-installers`` by default). The console then offers them from a
page every role can reach, and the download goes through the API so the
caller's session is checked like on any other route.

Only names that follow the bundle scheme are ever published or served::

    Asistente_<version>_<arch>.deb
    Asistente_<version>_<arch>.AppImage

``<version>`` is numeric (``0.1.0``), ``<arch>`` is alphanumeric
(``amd64``, ``aarch64``). Anything else in the directory — notes, a Windows
installer, a half-written file — is ignored, and a download of such a name is
a 404. The directory is read on every request: publishing a new build needs
no restart.
"""

import re
from dataclasses import dataclass
from pathlib import Path

#: The bundle scheme Tauri writes; anything else is not ours to publish.
_NAME = re.compile(r"^Asistente_(\d+(?:\.\d+)*)_([A-Za-z0-9]+)\.(deb|AppImage)$")

#: Media type per package type, so the browser saves the file instead of
#: rendering it and proxies/CDNs do not guess.
MEDIA_TYPES = {
    "deb": "application/vnd.debian.binary-package",
    "appimage": "application/octet-stream",
}


@dataclass(frozen=True)
class Installer:
    """One published bundle, parsed from its file name."""

    filename: str
    package_type: str
    version: str
    arch: str
    size_bytes: int
    path: Path


def parse_installer_name(filename: str) -> tuple[str, str, str] | None:
    """Return ``(package_type, version, arch)`` for a bundle name, else ``None``.

    ``package_type`` is the lower-case extension (``deb``/``appimage``). The
    match is strict and case-sensitive: a renamed or foreign file is not
    published by accident.
    """
    match = _NAME.fullmatch(filename)
    if match is None:
        return None
    version, arch, extension = match.groups()
    return extension.lower(), version, arch


def list_installers(directory: Path) -> list[Installer]:
    """Every publishable bundle in ``directory``, ordered by file name.

    A missing or unreadable directory is an empty list: the console shows
    "nothing published yet" instead of an error, and the app still starts on a
    machine where the widget was never built.
    """
    try:
        entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return []
    installers: list[Installer] = []
    for entry in entries:
        parsed = parse_installer_name(entry.name)
        if parsed is None:
            continue
        try:
            if not entry.is_file():
                continue
            size = entry.stat().st_size
        except OSError:
            # A file that vanished between listing and stat: skip it, the next
            # request sees the directory as it is then.
            continue
        package_type, version, arch = parsed
        installers.append(
            Installer(
                filename=entry.name,
                package_type=package_type,
                version=version,
                arch=arch,
                size_bytes=size,
                path=entry,
            )
        )
    return installers


def find_installer(directory: Path, filename: str) -> Installer | None:
    """The published bundle named ``filename``, or ``None``.

    The name is matched against the scheme before touching the filesystem, so
    a traversal attempt (``../x``) can never resolve to a path outside
    ``directory``.
    """
    if parse_installer_name(filename) is None:
        return None
    for installer in list_installers(directory):
        if installer.filename == filename:
            return installer
    return None
