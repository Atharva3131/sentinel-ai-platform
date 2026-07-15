"""Memory system exception hierarchy."""

from __future__ import annotations

from backend.exceptions import SentinelError


class MemoryError(SentinelError):
    """Base exception for memory system failures."""


class MemoryNotFoundError(MemoryError):
    """Raised when a requested memory entry does not exist."""


class MemoryScopeViolationError(MemoryError):
    """Raised when an operation violates scope boundaries."""


class MemorySerializationError(MemoryError):
    """Raised when serialization or deserialization of a memory entry fails."""


class MemoryStorageError(MemoryError):
    """Raised when a storage provider operation fails."""


class MemoryEvictionError(MemoryError):
    """Raised when the working memory eviction policy cannot free space."""
