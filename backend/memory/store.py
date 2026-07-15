"""MemoryStore — provider + serializer coordination with versioning and checksums."""

from __future__ import annotations

from dataclasses import dataclass

from backend.memory.models import MemoryEntry, MemoryScope
from backend.memory.providers import MemoryProvider
from backend.memory.serializer import MemorySerializer


@dataclass(slots=True)
class MemoryStore:
    """Coordinates read/write against a MemoryProvider with versioning and checksum logic.

    Handles version increment on every write and checksum computation.
    All scope routing uses composite ``{scope}:{scope_id}`` keys.
    """

    provider: MemoryProvider
    serializer: MemorySerializer

    def _scope_key(self, scope: MemoryScope, scope_id: str) -> str:
        return f"{scope}:{scope_id}"

    async def get(self, key: str, *, scope: MemoryScope, scope_id: str) -> MemoryEntry | None:
        raw = await self.provider.read(key, scope=self._scope_key(scope, scope_id))
        if raw is None:
            return None
        return self.serializer.deserialize(raw)

    async def put(self, entry: MemoryEntry) -> MemoryEntry:
        scope_key = self._scope_key(entry.metadata.scope, entry.metadata.scope_id)
        existing_raw = await self.provider.read(entry.key, scope=scope_key)
        if existing_raw is not None:
            try:
                existing = self.serializer.deserialize(existing_raw)
                next_version = existing.metadata.version + 1
            except Exception:
                next_version = entry.metadata.version
        else:
            next_version = entry.metadata.version

        versioned = entry.with_version(next_version)
        checksum = self.serializer.compute_checksum(versioned.value)
        final = versioned.with_checksum(checksum)
        serialized = self.serializer.serialize(final)
        await self.provider.write(
            entry.key,
            serialized,
            scope=scope_key,
            ttl_seconds=final.metadata.ttl_seconds,
        )
        return final

    async def remove(self, key: str, *, scope: MemoryScope, scope_id: str) -> bool:
        return await self.provider.delete(key, scope=self._scope_key(scope, scope_id))

    async def exists(self, key: str, *, scope: MemoryScope, scope_id: str) -> bool:
        return await self.provider.exists(key, scope=self._scope_key(scope, scope_id))

    async def search(
        self,
        *,
        scope: MemoryScope,
        scope_id: str,
        pattern: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[MemoryEntry]:
        raw_list = await self.provider.search(
            scope=self._scope_key(scope, scope_id),
            pattern=pattern,
            tags=tags,
            limit=limit,
        )
        results: list[MemoryEntry] = []
        for raw in raw_list:
            try:
                results.append(self.serializer.deserialize(raw))
            except Exception:
                continue
        return results

    async def list_keys(self, *, scope: MemoryScope, scope_id: str, limit: int = 100) -> list[str]:
        return await self.provider.list_keys(
            scope=self._scope_key(scope, scope_id),
            limit=limit,
        )
