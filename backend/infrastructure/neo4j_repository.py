"""Neo4j repository abstractions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from backend.infrastructure.neo4j import Neo4jCypherExecutor


@dataclass(slots=True)
class Neo4jRepositoryBase:
    """Storage-isolating repository base for Cypher-backed data access."""

    executor: Neo4jCypherExecutor

    async def read(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run a read-only Cypher query."""
        return await self.executor.read(query, parameters, database=database)

    async def write(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run a write Cypher query."""
        return await self.executor.write(query, parameters, database=database)

    async def single(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> dict[str, Any] | None:
        """Return at most one row from a Cypher query."""
        return await self.executor.single(query, parameters, database=database)

