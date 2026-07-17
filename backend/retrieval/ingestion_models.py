"""Ingestion value objects — versions, requests, results, and metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from backend.retrieval.models import DocumentSource


class IngestionStatus(StrEnum):
    """Outcome of a single document ingestion attempt."""

    CREATED = "created"
    UPDATED = "updated"
    SKIPPED = "skipped"
    FAILED = "failed"


class DocumentFormat(StrEnum):
    """Detected or declared format of raw document bytes."""

    PDF = "pdf"
    MARKDOWN = "markdown"
    JSON = "json"
    TEXT = "text"
    HTML = "html"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    """Immutable snapshot of a document's version state after a check.

    ``is_new`` is True only for documents that did not exist before.
    When ``previous_checksum == checksum`` the document content is unchanged.
    """

    document_id: str
    version: int
    checksum: str
    previous_version: int | None = None
    previous_checksum: str | None = None
    is_new: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def has_changed(self) -> bool:
        """Return True when the document is new or its content changed."""
        return self.is_new or self.previous_checksum != self.checksum


@dataclass(frozen=True, slots=True)
class ExtractedMetadata:
    """Structured metadata parsed from document content."""

    title: str | None = None
    author: str | None = None
    language: str | None = None
    tags: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    entity_refs: tuple[str, ...] = ()
    word_count: int | None = None
    summary: str | None = None
    custom: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraphAnchor:
    """A target graph node and the relationship type to create toward it.

    ``relationship_type`` must be one of the allowlisted document relationship
    types validated by RelationshipBuilder.
    """

    target_node_id: str
    relationship_type: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IngestionRequest:
    """Immutable specification for a single document ingestion operation."""

    document_id: str
    content: bytes | str
    content_type: str
    source: DocumentSource
    tenant_id: str | None = None
    author: str | None = None
    tags: tuple[str, ...] = ()
    graph_anchors: tuple[GraphAnchor, ...] = ()
    embedding_provider_name: str | None = None
    generate_embeddings: bool = False
    skip_if_unchanged: bool = True
    blob_upload: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Immutable summary of a completed ingestion pipeline run."""

    document_id: str
    status: IngestionStatus
    version: DocumentVersion | None = None
    chunk_count: int = 0
    blob_uri: str | None = None
    duration_ms: float | None = None
    message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status != IngestionStatus.FAILED
