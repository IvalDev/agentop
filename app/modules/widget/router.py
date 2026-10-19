"""Widget installer publication API (T7).

=================================================== ==============================
route                                               meaning
=================================================== ==============================
``GET /api/v1/widget/installers``                   published bundles
``GET /api/v1/widget/installers/{filename}``        the bundle itself
=================================================== ==============================

Both routes need an authenticated caller and no role in particular: everyone
who uses the chat may install the widget. The list is derived from the
publication directory on every request (see :mod:`app.modules.widget.service`);
an unpublished or foreign name is 404. Downloads carry
``Content-Disposition: attachment`` so the browser saves the file instead of
trying to open it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.core.config import Settings
from app.core.deps import get_settings
from app.modules.identity.deps import CurrentUser, get_current_user
from app.modules.widget.service import MEDIA_TYPES, find_installer, list_installers
from packages.contracts.schemas import (
    WidgetInstallerListResponse,
    WidgetInstallerResponse,
)

router = APIRouter(prefix="/api/v1/widget/installers", tags=["widget"])

CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("", response_model=WidgetInstallerListResponse)
async def list_widget_installers(
    _user: CurrentUserDep, settings: SettingsDep
) -> WidgetInstallerListResponse:
    """Return the published bundles, ordered by file name."""
    installers = list_installers(settings.widget_installers_path)
    return WidgetInstallerListResponse(
        installers=[
            WidgetInstallerResponse(
                filename=installer.filename,
                package_type=installer.package_type,
                version=installer.version,
                arch=installer.arch,
                size_bytes=installer.size_bytes,
                download_url=f"/api/v1/widget/installers/{installer.filename}",
            )
            for installer in installers
        ]
    )


@router.get("/{filename}", response_model=None)
async def download_widget_installer(
    filename: str, _user: CurrentUserDep, settings: SettingsDep
) -> FileResponse:
    """Serve one published bundle; anything else is 404."""
    installer = find_installer(settings.widget_installers_path, filename)
    if installer is None:
        raise HTTPException(status_code=404, detail="installer not found")
    return FileResponse(
        installer.path,
        media_type=MEDIA_TYPES[installer.package_type],
        filename=installer.filename,
        headers={"X-Content-Type-Options": "nosniff"},
    )
