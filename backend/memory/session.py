"""SessionMemory — Redis-backed memory fixed to SESSION scope."""

from __future__ import annotations

from dataclasses import dataclass, replace

from backend.memory.models import MemoryEntry, MemoryScope
from backend.memory.store import MemoryStore

_DEFAULT_SESSION_TTL_SECONDS: float = 86400.0
_SESSION_SCOPE = MemoryScope.SESSION


@dataclass(slots=True)
class SessionMemory:
    """Session-scoped memory backed by Redis.

    Scope is always MemoryScope.SESSION. scope_id is the session identifier.
    """

    store: MemoryStore
    default_ttl_seconds: float = _DEFAULT_SESSION_TTL_SECONDS

    async def get(self, key: str, *, session_id: str) -> MemoryEntry | None:
        return await self.store.get(key, scope=_SESSION_SCOPE, scope_id=session_id)

    async def put(self, entry: MemoryEntry, *, session_id: str) -> MemoryEntry:
        effective_ttl = (
            entry.metadata.ttl_seconds
            if entry.metadata.ttl_seconds is not None
            else self.default_ttl_seconds
        )
        metadata = replace(
            entry.metadata,
            scope=_SESSION_SCOPE,
            scope_id=session_id,
            ttl_seconds=effective_ttl,
        )
        return await self.store.put(replace(entry, metadata=metadata))

    async def remove(self, key: str, *, session_id: str) -> bool:
        return await self.store.remove(key, scope=_SESSION_SCOPE, scope_id=session_id)

    async def exists(self, key: str, *, session_id: str) -> bool:
        return await self.store.exists(key, scope=_SESSION_SCOPE, scope_id=session_id)

    async def search(
        self,
        *,
        session_id: str,
        pattern: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[MemoryEntry]:
        return await self.store.search(
            scope=_SESSION_SCOPE,
            scope_id=session_id,
            pattern=pattern,
            tags=tags,
            limit=limit,
        )

    async def list_keys(self, *, session_id: str, limit: int = 100) -> list[str]:
        return await self.store.list_keys(scope=_SESSION_SCOPE, scope_id=session_id, limit=limit)
