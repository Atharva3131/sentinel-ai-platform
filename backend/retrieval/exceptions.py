"""Retrieval subsystem exception hierarchy."""

from __future__ import annotations

from typing import Any

from backend.exceptions import SentinelError


class RetrievalError(SentinelError):
    """Base exception for retrieval subsystem failures."""


class DocumentProcessingError(RetrievalError):
    """Raised when document ingestion or validation fails."""

    def __init__(
        self,
        message: str,
        *,
        document_id: str | None = None,
        content_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.content_type = content_type
        self.metadata: dict[str, Any] = metadata or {}


class ChunkingError(RetrievalError):
    """Raised when document chunking fails."""

    def __init__(
        self,
        message: str,
        *,
        document_id: str | None = None,
        strategy: str | None = None,
    ) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.strategy = strategy


class EmbeddingError(RetrievalError):
    """Raised when embedding generation fails."""

    def __init__(
        self,
        message: str,
        *,
        provider_name: str | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_name = provider_name
        self.model = model
        self.metadata: dict[str, Any] = metadata or {}


class GraphTraversalError(RetrievalError):
    """Raised when a graph traversal query fails."""

    def __init__(
        self,
        message: str,
        *,
        anchor_id: str | None = None,
        depth: int | None = None,
    ) -> None:
        super().__init__(message)
        self.anchor_id = anchor_id
        self.depth = depth


class RetrievalProviderError(RetrievalError):
    """Raised when a RetrievalProvider operation fails."""

    def __init__(
        self,
        message: str,
        *,
        provider_name: str | None = None,
        strategy: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_name = provider_name
        self.strategy = strategy
        self.metadata: dict[str, Any] = metadata or {}


class ContextOptimizationError(RetrievalError):
    """Raised when context optimization fails."""


class KnowledgeManagerError(RetrievalError):
    """Raised when the KnowledgeManager cannot complete an operation."""
