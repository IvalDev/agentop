"""Shared pytest fixtures for the admin-agent test suite."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
def app() -> FastAPI:
    """Build a fresh application instance."""
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:
    """HTTP client bound to the application via ASGI transport."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
