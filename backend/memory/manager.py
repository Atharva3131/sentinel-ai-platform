"""MemoryManager — tiered get/put/search/promote/evict/summarize coordinator."""

from __future__ import annotations

from dataclasses import dataclass

from backend.memory.hooks import EvictionHook, SummarizationHook
from backend.memory.long_term import LongTermMemory
from backend.memory.models import MemoryEntry, MemoryScope
from backend.memory.short_term import ShortTermMemory
from backend.memory.working import WorkingMemory


@dataclass(slots=True)
class MemoryManager:
    """Coordinates working → short-term → long-term memory tiers.

    Read path: working → short-term → long-term (with optional promotion to short-term).
    Write path: EXECUTION/WORKFLOW scope → short-term; SESSION/GLOBAL scope → long-term.
    Working memory is always populated on reads and writes.
    """

    working: WorkingMemory
    short_term: ShortTermMemory
    long_term: LongTermMemory
    summarization_hook: SummarizationHook | None = None
    eviction_hook: EvictionHook | None = None

    async def get(
        self,
        key: str,
        *,
        scope: MemoryScope,
        scope_id: str,
        promote: bool = False,
    ) -> MemoryEntry | None:
        entry = self.working.get(key, scope=scope, scope_id=scope_id)
        if entry is not None:
            return entry

        entry = await self.short_term.get(key, scope=scope, scope_id=scope_id)
        if entry is not None:
            self.working.put(entry)
            return entry

        entry = await self.long_term.get(key, scope=scope, scope_id=scope_id)
        if entry is not None:
            if promote:
                await self.short_term.put(entry)
            self.working.put(entry)
            return entry

        return None

    async def put(self, entry: MemoryEntry) -> MemoryEntry:
        if entry.metadata.scope in (MemoryScope.EXECUTION, MemoryScope.WORKFLOW):
            final = await self.short_term.put(entry)
        else:
            final = await self.long_term.put(entry)
        self.working.put(final)
        return final

    async def remove(self, key: str, *, scope: MemoryScope, scope_id: str) -> bool:
        self.working.remove(key, scope=scope, scope_id=scope_id)
        if scope in (MemoryScope.EXECUTION, MemoryScope.WORKFLOW):
            return await self.short_term.remove(key, scope=scope, scope_id=scope_id)
        return await self.long_term.remove(key, scope=scope, scope_id=scope_id)

    async def exists(self, key: str, *, scope: MemoryScope, scope_id: str) -> bool:
        if self.working.exists(key, scope=scope, scope_id=scope_id):
            return True
        if scope in (MemoryScope.EXECUTION, MemoryScope.WORKFLOW):
            return await self.short_term.exists(key, scope=scope, scope_id=scope_id)
        return await self.long_term.exists(key, scope=scope, scope_id=scope_id)

    async def search(
        self,
        *,
        scope: MemoryScope,
        scope_id: str,
        pattern: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[MemoryEntry]:
        if scope in (MemoryScope.EXECUTION, MemoryScope.WORKFLOW):
            return await self.short_term.search(
                scope=scope, scope_id=scope_id, pattern=pattern, tags=tags, limit=limit
            )
        return await self.long_term.search(
            scope=scope, scope_id=scope_id, pattern=pattern, tags=tags, limit=limit
        )

    async def promote(self, key: str, *, scope: MemoryScope, scope_id: str) -> MemoryEntry | None:
        """Promote an entry from short-term to long-term storage."""
        entry = await self.short_term.get(key, scope=scope, scope_id=scope_id)
        if entry is None:
            return None
        return await self.long_term.put(entry)

    async def evict_working(self, *, scope: MemoryScope, scope_id: str) -> int:
        """Evict all working-memory entries for a scope, notifying the eviction hook."""
        entries = self.working.list_entries(scope=scope, scope_id=scope_id)
        if self.eviction_hook is not None:
            for entry in entries:
                await self.eviction_hook.on_evict(entry)
        return self.working.clear(scope=scope, scope_id=scope_id)

    async def summarize(self, *, scope: MemoryScope, scope_id: str) -> MemoryEntry | None:
        """Summarize working-memory entries via the configured SummarizationHook."""
        if self.summarization_hook is None:
            return None
        entries = self.working.list_entries(scope=scope, scope_id=scope_id)
        if not entries:
            return None
        summary = await self.summarization_hook.summarize(entries)
        return await self.put(summary)
