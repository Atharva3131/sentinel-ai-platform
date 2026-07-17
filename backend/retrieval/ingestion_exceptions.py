"""Exception hierarchy for the knowledge ingestion subsystem."""

from __future__ import annotations

from typing import Any

from backend.retrieval.exceptions import RetrievalError


class IngestionError(RetrievalError):
    """Base exception for ingestion subsystem failures."""


class ContentExtractionError(IngestionError):
    """Raised when raw bytes cannot be converted to text."""

    def __init__(
        self,
        message: str,
        *,
        document_id: str | None = None,
        content_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.content_type = content_type


class MetadataExtractionError(IngestionError):
    """Raised when structured metadata cannot be parsed from document content."""

    def __init__(
        self,
        message: str,
        *,
        document_id: str | None = None,
        content_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.content_type = content_type


class VersioningError(IngestionError):
    """Raised when version state cannot be read or written."""

    def __init__(
        self,
        message: str,
        *,
        document_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.document_id = document_id


class EmbeddingIndexError(IngestionError):
    """Raised when chunk embedding or index persistence fails."""

    def __init__(
        self,
        message: str,
        *,
        document_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.metadata: dict[str, Any] = metadata or {}


class GraphBuildError(IngestionError):
    """Raised when a Neo4j graph upsert or relationship creation fails."""

    def __init__(
        self,
        message: str,
        *,
        document_id: str | None = None,
        node_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.node_type = node_type


class RelationshipBuildError(IngestionError):
    """Raised when a Neo4j relationship template call fails."""

    def __init__(
        self,
        message: str,
        *,
        document_id: str | None = None,
        relationship_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.relationship_type = relationship_type


class IngestionPipelineError(IngestionError):
    """Raised when the ingestion pipeline fails at an orchestration level."""

    def __init__(
        self,
        message: str,
        *,
        document_id: str | None = None,
        stage: str | None = None,
    ) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.stage = stage
