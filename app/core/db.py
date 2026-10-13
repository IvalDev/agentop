"""Async database engine, session management, and health ping."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import Request
from sqlalchemy import DateTime, func, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model in the application."""


class TimestampMixin:
    """``created_at``/``updated_at`` columns maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def _engine_kwargs(url: str, pool_size: int, max_overflow: int) -> dict[str, Any]:
    """Build pool arguments; SQLite has no overflow pool to configure."""
    if url.startswith("sqlite"):
        return {}
    return {"pool_size": pool_size, "max_overflow": max_overflow}


class Database:
    """Holder for the async engine and its session factory."""

    def __init__(self, url: str, *, pool_size: int = 5, max_overflow: int = 10) -> None:
        self.engine: AsyncEngine = create_async_engine(
            url, **_engine_kwargs(url, pool_size, max_overflow)
        )
        self.sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    async def ping(self) -> bool:
        """Return ``True`` when the database answers a trivial query."""
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            logger.warning("database ping failed", exc_info=True)
            return False
        return True

    async def dispose(self) -> None:
        """Close the engine pool and release its connections."""
        await self.engine.dispose()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session from ``app.state.db``."""
    db: Database | None = getattr(request.app.state, "db", None)
    if db is None:
        raise RuntimeError("database is not initialized")
    async with db.sessionmaker() as session:
        yield session


@asynccontextmanager
async def session_scope(db: Database) -> AsyncIterator[AsyncSession]:
    """Worker context manager: commit on success, rollback and re-raise on error."""
    async with db.sessionmaker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
