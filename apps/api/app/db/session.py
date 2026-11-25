"""Async SQLAlchemy 2.x engine and session factory.

The module exposes a process-wide async engine built from the configured
``DATABASE_URL`` and an async sessionmaker used by FastAPI dependencies. The
``lifespan`` context manager creates/disposes the engine alongside the app.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def build_engine(settings: Settings) -> AsyncEngine:
    """Create an async engine using the configured database URL."""
    return create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        future=True,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


class DatabaseState:
    """Holds the process-wide engine and session factory."""

    def __init__(self) -> None:
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None

    async def init(self, settings: Settings) -> None:
        if self.engine is not None:
            return
        self.engine = build_engine(settings)
        self.session_factory = build_session_factory(self.engine)

    async def dispose(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
            self.session_factory = None


database_state = DatabaseState()


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a transactional async session."""
    if database_state.session_factory is None:
        raise RuntimeError("Database has not been initialised")
    async with database_state.session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


__all__ = [
    "AsyncSession",
    "database_state",
    "get_db",
]
