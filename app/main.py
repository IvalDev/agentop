"""Application entry point: FastAPI app factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import Settings
from app.core.logging import setup_logging


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Application lifespan stub; real wiring (DB, queue) lands in T2."""
    setup_logging()
    yield


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    settings = Settings()
    application = FastAPI(
        title="admin-agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.include_router(health_router)
    return application


app = create_app()
