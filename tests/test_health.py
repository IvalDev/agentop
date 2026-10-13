"""Health check endpoint tests."""

from fastapi import FastAPI
from httpx import AsyncClient

from app.core.db import Database


async def test_healthz_without_database_returns_ok(client: AsyncClient) -> None:
    """No database attached: endpoint stays 200 and reports database error."""
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "error"}


async def test_healthz_with_working_database_reports_ok(client: AsyncClient, app: FastAPI) -> None:
    """Reachable database: health reports database ok."""
    db = Database("sqlite+aiosqlite:///:memory:")
    app.state.db = db
    try:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "ok"}
    finally:
        await db.dispose()


async def test_healthz_reports_error_when_database_unreachable(
    client: AsyncClient, app: FastAPI
) -> None:
    """Unreachable database: status stays ok, database reports error."""
    app.state.db = Database("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none")
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "error"}
