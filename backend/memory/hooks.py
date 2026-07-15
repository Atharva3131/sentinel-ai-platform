"""Summarization and eviction hook Protocols for the memory system."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.memory.models import MemoryEntry


@runtime_checkable
class SummarizationHook(Protocol):
    """Called when a memory tier triggers summarization of entries."""

    async def summarize(self, entries: list[MemoryEntry]) -> MemoryEntry:
        """Compress a list of entries into a single summary entry."""
        ...


@runtime_checkable
class EvictionHook(Protocol):
    """Called just before an entry is evicted from working memory."""

    async def on_evict(self, entry: MemoryEntry) -> None:
        """Notification that an entry is about to be evicted."""
        ...
