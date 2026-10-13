"""Database engine, ping, and session tests (SQLite via aiosqlite)."""

import pytest
from sqlalchemy import text

from app.core.db import Base, Database, session_scope


@pytest.mark.asyncio
async def test_sqlite_engine_creates_metadata_and_pings() -> None:
    """An aiosqlite engine creates the metadata schema and answers pings."""
    db = Database("sqlite+aiosqlite:///:memory:")
    try:
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        assert await db.ping() is True
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_ping_returns_false_when_database_unreachable() -> None:
    """Ping reports failure instead of raising when the server is down."""
    db = Database("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none")
    try:
        assert await db.ping() is False
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_session_scope_yields_working_session() -> None:
    """session_scope yields a session that can execute SQL inside a transaction."""
    db = Database("sqlite+aiosqlite:///:memory:")
    try:
        async with session_scope(db) as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await db.dispose()
