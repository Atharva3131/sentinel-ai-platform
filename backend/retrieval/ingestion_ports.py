"""Protocol interfaces for the knowledge ingestion subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """Immutable record combining a Chunk with its embedding vector.

    Used as the unit written to a VectorIndexPort.
    """

    chunk_id: str
    document_id: str
    content: str
    index: int
    version: int
    embedding: tuple[float, ...] | None = None
    token_count: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class VectorIndexPort(Protocol):
    """Write-side port for a vector index (e.g., pgvector, Qdrant, Weaviate).

    Implementations own the serialization and index-specific operations.
    EmbeddingIndexer calls this port after generating embeddings; it does not
    know or care which vector database backs it.
    """

    async def upsert_chunks(
        self,
        document_id: str,
        chunks: list[ChunkRecord],
    ) -> int:
        """Upsert chunk records; return the count stored."""
        ...

    async def delete_by_document(self, document_id: str) -> int:
        """Delete all chunks for a document; return the count deleted."""
        ...


@runtime_checkable
class CacheInvalidator(Protocol):
    """Port for cache invalidation triggered after document ingestion.

    The production implementation scans Redis for keys matching the pattern
    and deletes them. The test double uses an in-memory set.
    """

    async def invalidate(self, pattern: str) -> int:
        """Delete all cache entries matching ``pattern``; return count deleted."""
        ...


@runtime_checkable
class Neo4jRepositoryPort(Protocol):
    """Structural port for Neo4j Cypher access used by graph builders.

    Matches ``Neo4jRepositoryBase`` structurally so test fakes satisfy mypy
    without inheriting from the concrete infrastructure class.
    """

    async def read(
        self,
        query: str,
        parameters: Any | None = None,
        *,
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run a read-only Cypher query."""
        ...

    async def write(
        self,
        query: str,
        parameters: Any | None = None,
        *,
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run a write Cypher query."""
        ...

    async def single(
        self,
        query: str,
        parameters: Any | None = None,
        *,
        database: str | None = None,
    ) -> dict[str, Any] | None:
        """Return at most one row from a Cypher query."""
        ...


@runtime_checkable
class KnowledgeEventPublisher(Protocol):
    """Port for emitting knowledge-graph lifecycle events.

    Implementations can publish to Redis Streams, message queues, or any
    other transport. Graph-updated and document-indexed events are emitted
    after successful ingestion so downstream consumers (e.g., search indexers,
    freshness monitors) can react without polling.
    """

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        tenant_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Publish a knowledge event with the given type and payload."""
        ...
