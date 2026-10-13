"""Application entry point: FastAPI app factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import Settings
from app.core.db import Database
from app.core.logging import setup_logging

logger = logging.getLogger(__name__)


def _log_level(name: str) -> int:
    """Resolve a log level name; unknown names fall back to ``INFO``."""
    return logging.getLevelNamesMapping().get(name.upper(), logging.INFO)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Configure logging, initialize the database, and dispose it on shutdown."""
    settings: Settings = application.state.settings
    setup_logging(level=_log_level(settings.log_level))

    db: Database | None = None
    if settings.database_url:
        db = Database(
            settings.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
        )
        if not await db.ping():
            logger.warning("database ping failed at startup; /healthz will report database error")
    else:
        logger.warning("AA_DATABASE_URL is not set; database features are disabled")
    application.state.db = db

    yield

    if db is not None:
        await db.dispose()


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    settings = Settings()
    application = FastAPI(
        title="admin-agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.db = None
    application.include_router(health_router)
    return application


app = create_app()
