"""Session and transaction management for SQLAlchemy."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create a reusable async session factory."""
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )


@dataclass(slots=True)
class DatabaseSessionManager:
    """Own the session factory and transaction helpers."""

    engine: AsyncEngine
    session_factory: Callable[[], AsyncSession]

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a managed database session."""
        async with self.session_factory() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Yield a session inside a SQLAlchemy transaction."""
        async with self.session() as session:
            async with session.begin():
                yield session

    async def run_in_transaction(self, operation: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
        """Execute a callback inside a transaction."""
        async with self.transaction() as session:
            return await operation(session)

    async def ping(self) -> None:
        """Check whether PostgreSQL accepts a simple round trip."""
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
