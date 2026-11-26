"""Fixtures for the Phase 3 PostgreSQL integration suite.

The tests use a real database and apply the Alembic migrations so that row
locking, constraints, triggers, and idempotency behave as in production.
Set ``TEST_DATABASE_URL`` to run them::

    TEST_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/db \
        pytest apps/api/tests/integration/ -v

Without ``TEST_DATABASE_URL`` every integration test is skipped cleanly,
keeping an ordinary ``pytest`` run hermetic. The suite owns the ``public``
schema and drops it at teardown; never point it at a shared or production
database.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

API_ROOT = Path(__file__).resolve().parents[2]  # .../apps/api

TABLES = (
    "credit_reservations",
    "credit_ledger",
    "credit_balances",
    "usage_events",
    "payments",
    "webhook_events",
    "audit_logs",
    "api_keys",
    "model_pricing",
    "users",
)


async def _reset_schema(url: str) -> None:
    """Drop and recreate ``public`` so Alembic starts from an empty schema."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


async def _drop_schema(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Return the integration database URL, skipping the suite when absent."""
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    return url


@pytest.fixture(scope="session")
def migrated_database(test_database_url: str) -> Iterator[str]:
    """Apply ``alembic upgrade head`` once per test session on a fresh schema.

    Alembic is invoked in a subprocess so its synchronous ``asyncio.run`` does
    not collide with the per-test event loop used by async fixtures.
    """
    asyncio.run(_reset_schema(test_database_url))

    env = os.environ.copy()
    env["DATABASE_URL"] = test_database_url
    # Placeholders satisfying mandatory Settings validation; no external
    # service is contacted.
    env.update(
        REDIS_URL="redis://localhost:6379/0",
        LITELLM_MASTER_KEY="integration-test-placeholder",
        JWT_SECRET="integration-test-placeholder",
        API_KEY_PEPPER="integration-test-placeholder",
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(API_ROOT / "alembic.ini"),
            "upgrade",
            "head",
        ],
        cwd=str(API_ROOT),
        env=env,
        check=True,
    )
    try:
        yield test_database_url
    finally:
        asyncio.run(_drop_schema(test_database_url))


@pytest_asyncio.fixture
async def session_factory(
    migrated_database: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Async session factory bound to a per-test ``NullPool`` engine.

    A fresh engine is created for each test so every connection belongs to the
    test's own event loop; a session-scoped engine would be bound to the first
    loop that touched it and fail on subsequent tests.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        yield async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _isolate(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Truncate every table before each test to keep state isolated."""
    async with session_factory() as session:
        await session.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
        await session.commit()
    yield
