"""Application entry point: FastAPI app factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.routing import Match, Route
from starlette.types import Scope

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

#: First path segments the web app never answers, so its client-side routes
#: cannot shadow the API, the dev console, health, or the API docs.
WEB_APP_RESERVED_SEGMENTS = frozenset({"api", "ui", "healthz", "docs", "redoc", "openapi.json"})

#: Vite puts content-hashed files under ``assets/``: safe to cache for a year.
_IMMUTABLE_CACHE = {"Cache-Control": "public, max-age=31536000, immutable"}


class _WebAppRoute(Route):
    """GET/HEAD catch-all for the web app that never matches reserved paths.

    Answering ``Match.NONE`` (instead of a partial match) leaves every other
    request to the router as before: 404 for unknown API paths, 405 only for
    real routes, and the trailing-slash redirects (``/ui`` → ``/ui/``).
    """

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] != "http" or scope["method"] not in ("GET", "HEAD"):
            return Match.NONE, {}
        match, child_scope = super().matches(scope)
        if match is Match.NONE:
            return Match.NONE, {}
        first_segment = child_scope["path_params"]["path"].split("/", 1)[0]
        if first_segment in WEB_APP_RESERVED_SEGMENTS:
            return Match.NONE, {}
        return match, child_scope


def _web_app_endpoint(dist_dir: Path):
    """Serve files from the build, else ``index.html`` for client-side routes."""
    root = dist_dir.resolve()
    index = root / "index.html"

    async def serve(request: Request) -> Response:
        relative: str = request.path_params["path"]
        if relative:
            candidate = (root / relative).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                headers = _IMMUTABLE_CACHE if relative.startswith("assets/") else None
                return FileResponse(candidate, headers=headers)
            # A missing file (an asset or anything with an extension) is a 404,
            # never the HTML page: client routes look like /chat, not /x.png.
            if relative.startswith("assets/") or "." in relative.rsplit("/", 1)[-1]:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
        # Always revalidate the page so a new build's asset names are picked up.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return serve


def _mount_web_app(application: FastAPI, dist_dir: Path) -> None:
    """Serve the built web app at ``/`` when ``dist_dir/index.html`` exists.

    Registered last, so every API route and mount keeps priority. Without a
    build (backend-only development, the test suite) nothing is mounted.
    """
    if not (dist_dir / "index.html").is_file():
        return
    application.router.routes.append(
        _WebAppRoute(
            "/{path:path}",
            _web_app_endpoint(dist_dir),
            methods=["GET"],
            include_in_schema=False,
        )
    )


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
    _mount_web_app(application, Path(settings.web_dist_dir))
    return application


app = create_app()
