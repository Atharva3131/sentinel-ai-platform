"""Optional PostgreSQL integration test fixtures.

These fixtures are only activated when the environment variable
``DATABASE_URL`` is set to a real PostgreSQL connection string.
When it is absent, all tests that depend on ``pg_session`` are
automatically skipped — no live database is required for CI.

Usage in a test::

    from backend.tests.conftest_db import pg_session

    @pytest.mark.asyncio
    async def test_real_save(pg_session):
        repo = PostgreSQLIncidentRepository(pg_session)
        ...
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import backend.db.models  # noqa: F401 — registers all ORM models with Base.metadata
from backend.db.base import Base

_DATABASE_URL = os.getenv("DATABASE_URL")
_SKIP_REASON = "DATABASE_URL environment variable is not set"


@pytest.fixture(scope="session")
async def pg_engine() -> AsyncIterator[AsyncEngine]:
    """Session-scoped async engine; skipped when DATABASE_URL is absent."""
    if _DATABASE_URL is None:
        pytest.skip(_SKIP_REASON)
    engine = create_async_engine(_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def pg_session(pg_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Function-scoped session wrapped in a rolled-back transaction."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    session_factory = async_sessionmaker(
        pg_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        async with session.begin():
            yield session
            await session.rollback()
