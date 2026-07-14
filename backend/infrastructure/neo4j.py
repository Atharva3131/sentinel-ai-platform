"""Neo4j driver, session, and Cypher execution abstractions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from neo4j import READ_ACCESS, WRITE_ACCESS, AsyncDriver, AsyncGraphDatabase, AsyncSession
from neo4j.exceptions import DriverError, ServiceUnavailable, SessionExpired

from backend.configuration.settings import Neo4jSettings
from backend.db.retry import RetryPolicy


def create_neo4j_driver(settings: Neo4jSettings) -> AsyncDriver:
    """Create an async Neo4j driver with pool and timeout settings."""
    return AsyncGraphDatabase.driver(
        settings.driver_uri,
        auth=(settings.username, settings.password.get_secret_value()),
        connection_timeout=settings.connection_timeout_seconds,
        max_connection_pool_size=settings.max_connection_pool_size,
    )


@dataclass(slots=True)
class Neo4jConnection:
    """Typed wrapper around the shared Neo4j driver."""

    driver: AsyncDriver
    database: str

    def session(self, *, database: str | None = None, **kwargs: Any) -> AsyncSession:
        """Create a session bound to the configured database."""
        return self.driver.session(database=database or self.database, **kwargs)

    async def verify_connectivity(self) -> None:
        """Verify that the driver can reach the configured database."""
        await self.driver.verify_connectivity(database=self.database)

    async def close(self) -> None:
        """Close the underlying driver gracefully."""
        await self.driver.close()


@dataclass(slots=True)
class Neo4jCypherExecutor:
    """Execute Cypher through the Neo4j driver with retry support."""

    connection: Neo4jConnection
    retry_policy: RetryPolicy

    async def read(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a read query and return materialized records."""
        return await self.retry_policy.run(
            lambda: self._execute(
                query,
                parameters,
                database=database,
                access_mode=READ_ACCESS,
            )
        )

    async def write(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a write query and return materialized records."""
        return await self.retry_policy.run(
            lambda: self._execute(
                query,
                parameters,
                database=database,
                access_mode=WRITE_ACCESS,
            )
        )

    async def single(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> dict[str, Any] | None:
        """Return at most one record from a Cypher query."""
        records = await self.read(query, parameters, database=database)
        return records[0] if records else None

    async def _execute(
        self,
        query: str,
        parameters: Mapping[str, Any] | None,
        *,
        database: str | None,
        access_mode: str,
    ) -> list[dict[str, Any]]:
        async with self.connection.session(
            database=database,
            default_access_mode=access_mode,
        ) as session:
            result = await session.run(query, dict(parameters or {}))
            return await result.data()


def create_neo4j_retry_policy() -> RetryPolicy:
    """Create a retry policy tuned for transient Neo4j connectivity errors."""
    return RetryPolicy(
        retryable_exceptions=(
            ServiceUnavailable,
            SessionExpired,
            DriverError,
            ConnectionError,
            TimeoutError,
            OSError,
        ),
    )
