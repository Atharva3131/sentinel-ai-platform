"""Memory system public API."""

from __future__ import annotations

from backend.memory.exceptions import (
    MemoryError,
    MemoryEvictionError,
    MemoryNotFoundError,
    MemoryScopeViolationError,
    MemorySerializationError,
    MemoryStorageError,
)
from backend.memory.hooks import EvictionHook, SummarizationHook
from backend.memory.long_term import LongTermMemory
from backend.memory.manager import MemoryManager
from backend.memory.models import (
    MemoryClassification,
    MemoryEntry,
    MemoryMetadata,
    MemoryScope,
)
from backend.memory.providers import CosmosMemoryProvider, MemoryProvider, RedisMemoryProvider
from backend.memory.serializer import MemorySerializer
from backend.memory.session import SessionMemory
from backend.memory.short_term import ShortTermMemory
from backend.memory.store import MemoryStore
from backend.memory.working import WorkingMemory

__all__ = [
    "CosmosMemoryProvider",
    "EvictionHook",
    "LongTermMemory",
    "MemoryClassification",
    "MemoryEntry",
    "MemoryError",
    "MemoryEvictionError",
    "MemoryManager",
    "MemoryMetadata",
    "MemoryNotFoundError",
    "MemoryProvider",
    "MemoryScope",
    "MemoryScopeViolationError",
    "MemorySerializationError",
    "MemorySerializer",
    "MemoryStorageError",
    "MemoryStore",
    "RedisMemoryProvider",
    "SessionMemory",
    "ShortTermMemory",
    "SummarizationHook",
    "WorkingMemory",
]
