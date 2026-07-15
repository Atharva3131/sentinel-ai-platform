"""WorkingMemory — bounded in-process memory with FIFO/expired eviction."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from backend.memory.models import MemoryEntry, MemoryScope


@dataclass(slots=True)
class WorkingMemory:
    """Bounded, in-process memory for within-execution transient data.

    Eviction policy: expired entries first; when still at capacity, FIFO (oldest first).
    Not safe for concurrent access across threads — single asyncio event loop only.
    """

    max_entries: int = 256
    _store: OrderedDict[str, MemoryEntry] = field(default_factory=OrderedDict)

    def _full_key(self, key: str, scope: MemoryScope, scope_id: str) -> str:
        return f"{scope}:{scope_id}:{key}"

    def put(self, entry: MemoryEntry) -> None:
        full_key = self._full_key(entry.key, entry.metadata.scope, entry.metadata.scope_id)
        if full_key in self._store:
            del self._store[full_key]
        if len(self._store) >= self.max_entries:
            self._evict_one()
        self._store[full_key] = entry

    def get(self, key: str, *, scope: MemoryScope, scope_id: str) -> MemoryEntry | None:
        full_key = self._full_key(key, scope, scope_id)
        entry = self._store.get(full_key)
        if entry is None:
            return None
        if entry.is_expired():
            del self._store[full_key]
            return None
        return entry

    def remove(self, key: str, *, scope: MemoryScope, scope_id: str) -> bool:
        full_key = self._full_key(key, scope, scope_id)
        if full_key in self._store:
            del self._store[full_key]
            return True
        return False

    def exists(self, key: str, *, scope: MemoryScope, scope_id: str) -> bool:
        return self.get(key, scope=scope, scope_id=scope_id) is not None

    def list_entries(self, *, scope: MemoryScope, scope_id: str) -> list[MemoryEntry]:
        prefix = f"{scope}:{scope_id}:"
        now = datetime.now(UTC)
        return [
            e
            for k, e in list(self._store.items())
            if k.startswith(prefix) and not e.is_expired(now=now)
        ]

    def evict_expired(self) -> int:
        now = datetime.now(UTC)
        expired = [k for k, e in list(self._store.items()) if e.is_expired(now=now)]
        for k in expired:
            del self._store[k]
        return len(expired)

    def clear(self, *, scope: MemoryScope, scope_id: str) -> int:
        prefix = f"{scope}:{scope_id}:"
        keys = [k for k in list(self._store.keys()) if k.startswith(prefix)]
        for k in keys:
            del self._store[k]
        return len(keys)

    def _evict_one(self) -> None:
        now = datetime.now(UTC)
        for key, entry in list(self._store.items()):
            if entry.is_expired(now=now):
                del self._store[key]
                return
        self._store.popitem(last=False)

    @property
    def size(self) -> int:
        return len(self._store)
