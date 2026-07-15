"""LongTermMemory — Cosmos DB-backed memory for persistent cross-session data."""

from __future__ import annotations

from dataclasses import dataclass

from backend.memory.models import MemoryEntry, MemoryScope
from backend.memory.store import MemoryStore


@dataclass(slots=True)
class LongTermMemory:
    """Wraps a Cosmos DB-backed MemoryStore for persistent memory.

    Intended scopes: SESSION and GLOBAL. No automatic TTL unless set on the entry.
    """

    store: MemoryStore

    async def get(self, key: str, *, scope: MemoryScope, scope_id: str) -> MemoryEntry | None:
        return await self.store.get(key, scope=scope, scope_id=scope_id)

    async def put(self, entry: MemoryEntry) -> MemoryEntry:
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
