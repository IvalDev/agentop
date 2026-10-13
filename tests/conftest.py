"""Shared pytest fixtures for the admin-agent test suite."""

from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import Base, Database
from app.main import create_app
from app.modules.identity.deps import CurrentUser
from app.modules.identity.models import Tenant, User, UserRole
from app.modules.identity.security import hash_password
from app.modules.tools.registry import ApprovalPolicy, ToolKind, ToolRegistry, ToolSpec


@pytest.fixture(autouse=True)
def default_embedding_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never call a live Ollama: default every app to the fake provider.

    ``create_app`` builds the embedding adapter from ``Settings``; fixtures
    that build an app after this runs pick up the offline fake. Tests that
    need a specific provider override ``app.state.embeddings`` explicitly
    (constructor arguments still beat environment variables in pydantic).
    """
    monkeypatch.setenv("AA_EMBEDDING_PROVIDER", "fake")


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


@pytest.fixture
async def seeded_users(seeded_db: Database) -> dict[str, CurrentUser]:
    """Request-scoped principals for every seeded user, keyed by email."""
    async with seeded_db.sessionmaker() as session:
        users = (await session.execute(select(User))).scalars().all()
    return {
        user.email: CurrentUser(
            id=user.id,
            tenant_id=user.tenant_id,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            is_superadmin=user.is_superadmin,
        )
        for user in users
    }


class SendNoteInput(BaseModel):
    """Input of the test-only ``send_note`` ACTION tool (``priority`` has a default)."""

    model_config = ConfigDict(extra="forbid")

    to: str = Field(min_length=1)
    body: str = Field(min_length=1)
    priority: int = Field(default=1, ge=1, le=5)


@dataclass
class ActionSpy:
    """Async handler double: records every call, then returns or raises."""

    error: Exception | None = None
    calls: list[tuple[int, CurrentUser, BaseModel]] = field(default_factory=list)

    async def __call__(self, session, tenant_id, user, validated_input) -> dict:
        self.calls.append((tenant_id, user, validated_input))
        if self.error is not None:
            raise self.error
        return {"delivered": True, "to": validated_input.to}


@dataclass
class ApprovalTools:
    """Local registry holding test-only tools (never the production registry)."""

    registry: ToolRegistry
    spy: ActionSpy


@pytest.fixture
def approval_tools() -> ApprovalTools:
    """Registry with an admin-gated ACTION tool and an AUTO extract tool."""
    registry = ToolRegistry()
    spy = ActionSpy()
    registry.register(
        ToolSpec(
            name="send_note",
            description="test-only action that needs human approval",
            kind=ToolKind.ACTION,
            approval_policy=ApprovalPolicy.REQUIRE_APPROVAL,
            required_role=UserRole.ADMIN,
            handler=spy,
            input_model=SendNoteInput,
        )
    )
    registry.register(
        ToolSpec(
            name="peek_note",
            description="test-only extract that never needs approval",
            kind=ToolKind.EXTRACT,
            approval_policy=ApprovalPolicy.AUTO,
            required_role=UserRole.VIEWER,
            handler=spy,
            input_model=SendNoteInput,
        )
    )
    return ApprovalTools(registry=registry, spy=spy)
