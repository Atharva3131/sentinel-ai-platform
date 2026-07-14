"""Azure Cosmos DB client, container, and partition abstractions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from azure.cosmos.aio import ContainerProxy, CosmosClient, DatabaseProxy
from azure.cosmos.partition_key import PartitionKey

from backend.configuration.settings import CosmosSettings


def create_cosmos_client(
    settings: CosmosSettings,
    *,
    credential: Any | None = None,
) -> CosmosClient:
    """Create an async Cosmos client with configured preferred locations."""
    if settings.connection_string is not None:
        return CosmosClient.from_connection_string(
            settings.connection_string.get_secret_value(),
            preferred_locations=settings.preferred_locations,
        )
    if credential is None:
        raise ValueError("credential is required when cosmos connection_string is not configured")
    return CosmosClient(
        settings.endpoint or "",
        credential=credential,
        preferred_locations=settings.preferred_locations,
    )


@dataclass(slots=True)
class CosmosConnection:
    """Typed wrapper around the shared Cosmos client."""

    client: CosmosClient

    def database(self, name: str) -> DatabaseProxy:
        """Return a lazy database client."""
        return self.client.get_database_client(name)

    async def ensure_database(self, name: str) -> DatabaseProxy:
        """Create the database if missing and return its client."""
        return await self.client.create_database_if_not_exists(name)

    async def close(self) -> None:
        """Close the underlying client gracefully."""
        close = getattr(self.client, "aclose", None) or getattr(self.client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result


@dataclass(frozen=True, slots=True)
class CosmosPartitionStrategy:
    """Partition key path and extraction strategy for Cosmos documents."""

    path: str = "/partitionKey"

    @property
    def normalized_path(self) -> str:
        """Return a Cosmos-compatible partition key path."""
        return self.path if self.path.startswith("/") else f"/{self.path}"

    @property
    def segments(self) -> tuple[str, ...]:
        """Return the document path segments used to extract a partition value."""
        return tuple(segment for segment in self.normalized_path.split("/") if segment)

    def container_partition_key(self) -> PartitionKey:
        """Return the Cosmos container partition key definition."""
        return PartitionKey(self.normalized_path)

    def value_from_document(self, document: Mapping[str, Any]) -> Any:
        """Extract a partition key value from a document."""
        current: Any = document
        for segment in self.segments:
            if not isinstance(current, Mapping) or segment not in current:
                raise KeyError(
                    f"partition key path {self.normalized_path!r} not present in document"
                )
            current = current[segment]
        return current


@dataclass(slots=True)
class CosmosContainerFactory:
    """Factory for lazily resolving Cosmos database and container clients."""

    connection: CosmosConnection
    database_name: str
    container_name: str
    partition_strategy: CosmosPartitionStrategy

    def database(self) -> DatabaseProxy:
        """Return a lazy database client."""
        return self.connection.database(self.database_name)

    def container(self) -> ContainerProxy:
        """Return a lazy container client."""
        return self.database().get_container_client(self.container_name)

    async def ensure_database(self) -> DatabaseProxy:
        """Create the database if missing and return its client."""
        return await self.connection.ensure_database(self.database_name)

    async def ensure_container(self) -> ContainerProxy:
        """Create the container if missing and return its client."""
        database = await self.ensure_database()
        return await database.create_container_if_not_exists(
            id=self.container_name,
            partition_key=self.partition_strategy.container_partition_key(),
        )
