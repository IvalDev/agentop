"""Application lifespan shutdown behaviour.

``uvicorn --reload`` runs startup and shutdown on every file change, so a
shutdown that raises (``Application shutdown failed. Exiting.``) aborts the
reloading server even when the next start would have succeeded.
"""

from fastapi import FastAPI

from app.core.config import Settings
from app.main import lifespan
from app.modules.connectors.hub import ConnectorHub


class _SpyHub(ConnectorHub):
    """Hub that records whether shutdown closed it."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    async def close_all(self) -> None:
        self.closed = True
        await super().close_all()


def _application() -> FastAPI:
    """App wired exactly like ``lifespan`` expects, without a database."""
    application = FastAPI()
    application.state.settings = Settings(database_url="")
    return application


async def test_shutdown_closes_the_connector_hub() -> None:
    """Normal path: the live hub is closed when the app stops."""
    application = _application()
    hub = _SpyHub()
    application.state.connectors = hub

    async with lifespan(application):
        assert application.state.db is None

    assert hub.closed is True


async def test_shutdown_without_connector_hub_still_completes() -> None:
    """No hub state (half-built app): shutdown must not raise."""
    application = _application()

    async with lifespan(application):
        assert application.state.db is None
