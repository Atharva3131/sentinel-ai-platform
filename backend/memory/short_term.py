"""ShortTermMemory — Redis-backed memory with configurable default TTL."""

from __future__ import annotations

from dataclasses import dataclass, replace

from backend.memory.models import MemoryEntry, MemoryScope
from backend.memory.store import MemoryStore

_DEFAULT_TTL_SECONDS: float = 3600.0


@dataclass(slots=True)
class ShortTermMemory:
    """Wraps a Redis-backed MemoryStore with a default TTL for transient entries.

    Intended scopes: EXECUTION and WORKFLOW.
    """

    store: MemoryStore
    default_ttl_seconds: float = _DEFAULT_TTL_SECONDS

    async def get(self, key: str, *, scope: MemoryScope, scope_id: str) -> MemoryEntry | None:
        return await self.store.get(key, scope=scope, scope_id=scope_id)

    async def put(self, entry: MemoryEntry) -> MemoryEntry:
        if entry.metadata.ttl_seconds is None:
            metadata = replace(entry.metadata, ttl_seconds=self.default_ttl_seconds)
            entry = replace(entry, metadata=metadata)
        return await self.store.put(entry)

    async def remove(self, key: str, *, scope: MemoryScope, scope_id: str) -> bool:
        return await self.store.remove(key, scope=scope, scope_id=scope_id)

    async def exists(self, key: str, *, scope: MemoryScope, scope_id: str) -> bool:
        return await self.store.exists(key, scope=scope, scope_id=scope_id)

    async def search(
        self,
        *,
        scope: MemoryScope,
        scope_id: str,
        pattern: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[MemoryEntry]:
        return await self.store.search(
            scope=scope, scope_id=scope_id, pattern=pattern, tags=tags, limit=limit
        )

    async def list_keys(self, *, scope: MemoryScope, scope_id: str, limit: int = 100) -> list[str]:
        return await self.store.list_keys(scope=scope, scope_id=scope_id, limit=limit)
