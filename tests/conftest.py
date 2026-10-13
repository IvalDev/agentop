"""Shared pytest fixtures for the admin-agent test suite."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.db import Base, Database
from app.main import create_app
from app.modules.identity.models import Tenant, User, UserRole
from app.modules.identity.security import hash_password


@pytest.fixture
def app() -> FastAPI:
    """Build a fresh application instance without a database attached."""
    application = create_app()
    application.state.db = None
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:
    """HTTP client bound to the application via ASGI transport."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
def identity_settings() -> Settings:
    """Settings with a fixed JWT secret so tokens are decodable in tests."""
    return Settings(
        jwt_secret="test-secret-0123456789-abcdefghijklmnop",
        access_token_expire_minutes=30,
    )


@pytest.fixture
async def identity_db() -> Database:
    """In-memory SQLite database with the full ORM schema created."""
    db = Database("sqlite+aiosqlite:///:memory:")
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
def identity_app(seeded_db: Database, identity_settings: Settings) -> FastAPI:
    """Application wired with the seeded test database and settings."""
    application = create_app()
    application.state.db = seeded_db
    application.state.settings = identity_settings
    return application


@pytest.fixture
async def identity_client(identity_app: FastAPI) -> AsyncClient:
    """HTTP client bound to the identity-enabled application."""
    transport = ASGITransport(app=identity_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
async def seeded_db(identity_db: Database) -> Database:
    """Database with two tenants and users of several roles seeded."""
    async with identity_db.sessionmaker() as session:
        acme = Tenant(name="Acme", slug="acme", is_active=True)
        globex = Tenant(name="Globex", slug="globex", is_active=True)
        session.add_all([acme, globex])
        await session.flush()
        session.add_all(
            [
                User(
                    tenant_id=acme.id,
                    email="owner@acme.test",
                    full_name="Acme Owner",
                    hashed_password=hash_password("owner-pass"),
                    role=UserRole.OWNER,
                ),
                User(
                    tenant_id=acme.id,
                    email="admin@acme.test",
                    full_name="Acme Admin",
                    hashed_password=hash_password("admin-pass"),
                    role=UserRole.ADMIN,
                ),
                User(
                    tenant_id=acme.id,
                    email="viewer@acme.test",
                    full_name="Acme Viewer",
                    hashed_password=hash_password("viewer-pass"),
                    role=UserRole.VIEWER,
                ),
                User(
                    tenant_id=globex.id,
                    email="owner@globex.test",
                    full_name="Globex Owner",
                    hashed_password=hash_password("other-pass"),
                    role=UserRole.OWNER,
                ),
            ]
        )
        await session.commit()
    return identity_db
