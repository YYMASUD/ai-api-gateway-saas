"""Lazy async database access for worker jobs.

The engine and sessionmaker are created on first use from ``DATABASE_URL``;
importing this module performs no I/O and requires no environment variables.
``NullPool`` is used because ARQ workers are long-lived processes that should
not retain idle PostgreSQL connections between jobs. A job that needs the
API's ledger services can do::

    from app.services.credit_service import CreditService
    from worker_app.db import session_scope

    async with session_scope() as session:
        service = CreditService(session)
        ...  # the context manager commits, or rolls back on error
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from worker_app.config import WorkerSettings as EnvWorkerSettings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first call only."""
    global _engine
    if _engine is None:
        settings = EnvWorkerSettings()  # type: ignore[call-arg]  # read from env.
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            poolclass=NullPool,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide sessionmaker bound to the lazy engine."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def dispose_engine() -> None:
    """Release the cached engine and session factory (e.g. on shutdown)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield an async session, committing on success and rolling back on error."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


__all__ = ["dispose_engine", "get_engine", "get_session_factory", "session_scope"]
