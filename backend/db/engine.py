"""SQLAlchemy engine construction for PostgreSQL."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from backend.configuration.settings import PostgresSettings


def create_postgres_engine(settings: PostgresSettings) -> AsyncEngine:
    """Create a PostgreSQL engine with production-safe pooling defaults."""
    return create_async_engine(
        settings.sqlalchemy_url,
        pool_pre_ping=True,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        pool_recycle=settings.pool_recycle_seconds,
    )
