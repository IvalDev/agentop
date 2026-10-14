"""Application entry point: FastAPI app factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.health import router as health_router
from app.core.config import Settings
from app.core.db import Database
from app.core.logging import setup_logging
from app.modules.agent.llm import build_llm_provider
from app.modules.agent.router import router as chat_router
from app.modules.approvals.router import router as approvals_router
from app.modules.audit.router import router as audit_router
from app.modules.audit.service import configure_audit
from app.modules.documents.router import router as documents_router
from app.modules.documents.storage import LocalDocumentStorage
from app.modules.identity.router import router as auth_router
from app.modules.identity.users_router import router as users_router
from app.modules.retrieval.embeddings import build_embedding_provider
from app.modules.retrieval.router import router as search_router
from app.modules.tools import builtin  # noqa: F401  (registers the production tools)
from app.modules.tools.registry import default_registry

logger = logging.getLogger(__name__)

#: HTML + JavaScript of the development console (no build step).
UI_STATIC_DIR = Path(__file__).parent / "ui" / "static"


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
    configure_audit(settings)
    application.state.storage = LocalDocumentStorage(settings.storage_base_dir)
    application.state.embeddings = build_embedding_provider(settings)
    # Fails fast on a misconfigured or unapproved external chat provider.
    application.state.llm = build_llm_provider(settings)
    application.state.tool_registry = default_registry
    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(users_router)
    application.include_router(documents_router)
    application.include_router(search_router)
    application.include_router(approvals_router)
    application.include_router(audit_router)
    application.include_router(chat_router)
    if settings.is_development:
        # Static dev console over the public API; any other environment answers 404.
        application.mount("/ui", StaticFiles(directory=UI_STATIC_DIR, html=True), name="ui")
    return application


app = create_app()
