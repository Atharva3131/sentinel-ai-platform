"""Retrieval value objects — documents, chunks, results, and enumerations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class DocumentSource(StrEnum):
    """Origin of a retrieved document."""

    BLOB = "blob"
    DATABASE = "database"
    GRAPH = "graph"
    CACHE = "cache"
    INLINE = "inline"


class ChunkStrategy(StrEnum):
    """Text splitting strategy used by ChunkingEngine."""

    FIXED = "fixed"
    SENTENCE = "sentence"
    PARAGRAPH = "paragraph"
    SEMANTIC = "semantic"  # reserved for future embedding-based splitting


class RetrievalStrategy(StrEnum):
    """Retrieval algorithm applied during a RetrievalPipeline run."""

    VECTOR = "vector"
    KEYWORD = "keyword"
    HYBRID = "hybrid"
    GRAPH = "graph"
    EXACT = "exact"


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    """Immutable provenance envelope for a raw document."""

    document_id: str
    source: DocumentSource
    uri: str | None = None
    content_type: str | None = None
    byte_size: int | None = None
    checksum: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    author: str | None = None
    tenant_id: str | None = None
    tags: tuple[str, ...] = ()
    custom: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Document:
    """Immutable container for processed document content."""

    document_id: str
    content: str
    metadata: DocumentMetadata
    embedding: tuple[float, ...] | None = None

    @property
    def source(self) -> DocumentSource:
        return self.metadata.source


@dataclass(frozen=True, slots=True)
class Chunk:
    """Immutable text segment produced by ChunkingEngine.

    ``chunk_id`` is deterministic: ``{document_id}:chunk:{index}``.
    ``char_offset`` is the byte offset of the chunk's first character in the
    original document content (None when not tracked).
    """

    chunk_id: str
    document_id: str
    content: str
    index: int
    token_count: int | None = None
    char_offset: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: tuple[float, ...] | None = None

    @classmethod
    def make(
        cls,
        document_id: str,
        content: str,
        index: int,
        *,
        char_offset: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Chunk:
        chunk_id = f"{document_id}:chunk:{index}"
        token_count = max(len(content) // 4, 1)
        return cls(
            chunk_id=chunk_id,
            document_id=document_id,
            content=content,
            index=index,
            token_count=token_count,
            char_offset=char_offset,
            metadata=metadata or {},
        )


@dataclass(frozen=True, slots=True)
class RetrievalMetadata:
    """Observability envelope for a single RetrievalPipeline execution."""

    retrieval_id: str
    strategy: RetrievalStrategy
    provider_name: str
    provider_version: str | None = None
    query_embedding_model: str | None = None
    latency_ms: float | None = None
    total_candidates: int | None = None
    cached: bool = False
    graph_depth: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Immutable retrieval hit — a ranked Chunk with relevance metadata.

    ``score`` is the normalized 0.0-1.0 relevance score assigned by the provider.
    ``provenance`` carries provider-specific origin information (e.g., graph path,
    blob URI, or vector distance).
    """

    result_id: str
    chunk: Chunk
    score: float
    source: DocumentSource
    provenance: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.score <= 1.0):
            raise ValueError(
                f"RetrievalResult score must be 0.0-1.0, got {self.score}"
            )

    @classmethod
    def from_chunk(
        cls,
        chunk: Chunk,
        score: float,
        *,
        retrieval_id: str,
        source: DocumentSource,
        provenance: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        result_id = f"{retrieval_id}:{chunk.chunk_id}"
        return cls(
            result_id=result_id,
            chunk=chunk,
            score=score,
            source=source,
            provenance=provenance,
            metadata=metadata or {},
        )
