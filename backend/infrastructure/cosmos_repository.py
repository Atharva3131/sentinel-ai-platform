"""Cosmos repository abstractions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from azure.cosmos.aio import ContainerProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from backend.db.retry import RetryPolicy
from backend.infrastructure.cosmos import CosmosPartitionStrategy


@dataclass(slots=True)
class CosmosRepositoryBase:
    """Storage-isolating repository base for Cosmos DB documents."""

    container: ContainerProxy
    partition_strategy: CosmosPartitionStrategy
    retry_policy: RetryPolicy | None = None

    async def _run[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        if self.retry_policy is None:
            return await operation()
        return await self.retry_policy.run(operation)

    def partition_key_from_document(self, document: Mapping[str, Any]) -> Any:
        """Extract the partition key value from a document."""
        return self.partition_strategy.value_from_document(document)

    async def create(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Create a new document."""
        return cast(
            dict[str, Any],
            await self._run(lambda: self.container.create_item(body=dict(document))),
        )

    async def upsert(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Insert or replace a document."""
        return cast(
            dict[str, Any],
            await self._run(lambda: self.container.upsert_item(body=dict(document))),
        )

    async def get(self, item_id: str, partition_key: Any) -> dict[str, Any] | None:
        """Read a document by item ID and partition key."""

        async def operation() -> dict[str, Any] | None:
            try:
                return cast(
                    dict[str, Any],
                    await self.container.read_item(item=item_id, partition_key=partition_key),
                )
            except CosmosResourceNotFoundError:
                return None

        return await self._run(operation)

    async def get_for_document(
        self,
        item_id: str,
        document: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Read a document using the partition key extracted from another document."""
        return await self.get(item_id, self.partition_key_from_document(document))

    async def delete(self, item_id: str, partition_key: Any) -> dict[str, Any] | None:
        """Delete a document by item ID and partition key."""
        async def operation() -> dict[str, Any] | None:
            return cast(
                dict[str, Any] | None,
                await self.container.delete_item(
                    item=item_id,
                    partition_key=partition_key,
                ),
            )

        return await self._run(operation)

    async def query(
        self,
        query: str,
        *,
        partition_key: Any | None = None,
        parameters: Sequence[dict[str, Any]] | None = None,
        max_item_count: int | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a SQL-like query and materialize the results."""

        async def operation() -> list[dict[str, Any]]:
            iterator = self.container.query_items(
                query=query,
                parameters=list(parameters) if parameters is not None else None,
                partition_key=partition_key,
                max_item_count=max_item_count,
            )
            documents: list[dict[str, Any]] = []
            async for document in cast(Any, iterator):
                documents.append(cast(dict[str, Any], document))
            return documents

        return await self._run(operation)
