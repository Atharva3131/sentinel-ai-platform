"""Memory system unit tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from backend.memory.long_term import LongTermMemory
from backend.memory.manager import MemoryManager
from backend.memory.models import (
    MemoryClassification,
    MemoryEntry,
    MemoryMetadata,
    MemoryScope,
)
from backend.memory.serializer import MemorySerializer
from backend.memory.session import SessionMemory
from backend.memory.short_term import ShortTermMemory
from backend.memory.store import MemoryStore
from backend.memory.working import WorkingMemory

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeMemoryProvider:
    """In-memory MemoryProvider test double."""

    _data: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _key(self, key: str, scope: str) -> str:
        return f"{scope}::{key}"

    async def read(self, key: str, *, scope: str) -> dict[str, Any] | None:
        return self._data.get(self._key(key, scope))

    async def write(
        self,
        key: str,
        data: dict[str, Any],
        *,
        scope: str,
        ttl_seconds: float | None = None,
    ) -> None:
        self._data[self._key(key, scope)] = data

    async def delete(self, key: str, *, scope: str) -> bool:
        k = self._key(key, scope)
        if k in self._data:
            del self._data[k]
            return True
        return False

    async def exists(self, key: str, *, scope: str) -> bool:
        return self._key(key, scope) in self._data

    async def search(
        self,
        *,
        scope: str,
        pattern: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        prefix = f"{scope}::"
        results = []
        for k, v in self._data.items():
            if not k.startswith(prefix):
                continue
            entry_key = str(v.get("key", ""))
            if pattern and pattern not in entry_key:
                continue
            if tags:
                entry_tags = set(v.get("metadata", {}).get("tags", []))
                if not all(t in entry_tags for t in tags):
                    continue
            results.append(v)
            if len(results) >= limit:
                break
        return results

    async def list_keys(self, *, scope: str, limit: int = 100) -> list[str]:
        prefix = f"{scope}::"
        return [
            k[len(prefix):]
            for k in self._data
            if k.startswith(prefix)
        ][:limit]


@dataclass(slots=True)
class RecordingEvictionHook:
    evicted: list[str] = field(default_factory=list)

    async def on_evict(self, entry: MemoryEntry) -> None:
        self.evicted.append(entry.key)


@dataclass(slots=True)
class PassthroughSummarizationHook:
    async def summarize(self, entries: list[MemoryEntry]) -> MemoryEntry:
        first = entries[0]
        return MemoryEntry.create(
            key="summary",
            value={"count": len(entries)},
            metadata=first.metadata,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _metadata(
    scope: MemoryScope = MemoryScope.EXECUTION,
    scope_id: str = "exec-1",
    tags: tuple[str, ...] = (),
    ttl_seconds: float | None = None,
) -> MemoryMetadata:
    return MemoryMetadata(
        scope=scope,
        scope_id=scope_id,
        tags=tags,
        ttl_seconds=ttl_seconds,
    )


def _entry(
    key: str = "k1",
    value: Any = None,
    scope: MemoryScope = MemoryScope.EXECUTION,
    scope_id: str = "exec-1",
    tags: tuple[str, ...] = (),
    ttl_seconds: float | None = None,
) -> MemoryEntry:
    return MemoryEntry.create(
        key=key,
        value=value if value is not None else {"data": "hello"},
        metadata=_metadata(scope=scope, scope_id=scope_id, tags=tags, ttl_seconds=ttl_seconds),
    )


def _store(provider: FakeMemoryProvider | None = None) -> MemoryStore:
    return MemoryStore(
        provider=provider or FakeMemoryProvider(),
        serializer=MemorySerializer(),
    )


# ---------------------------------------------------------------------------
# MemorySerializer
# ---------------------------------------------------------------------------


def test_serializer_roundtrip() -> None:
    serializer = MemorySerializer()
    entry = _entry()
    data = serializer.serialize(entry)
    recovered = serializer.deserialize(data)
    assert recovered.key == entry.key
    assert recovered.value == entry.value
    assert recovered.metadata.scope == entry.metadata.scope


def test_serializer_checksum_is_deterministic() -> None:
    serializer = MemorySerializer()
    c1 = serializer.compute_checksum({"a": 1})
    c2 = serializer.compute_checksum({"a": 1})
    assert c1 == c2
    assert len(c1) == 16


def test_serializer_json_roundtrip() -> None:
    serializer = MemorySerializer()
    entry = _entry()
    raw = serializer.to_json(entry)
    recovered = serializer.from_json(raw)
    assert recovered.key == entry.key


# ---------------------------------------------------------------------------
# MemoryEntry
# ---------------------------------------------------------------------------


def test_entry_not_expired_by_default() -> None:
    entry = _entry()
    assert not entry.is_expired()


def test_entry_expired_when_past_expires_at() -> None:
    past = datetime.now(UTC) - timedelta(seconds=1)
    entry = _entry()
    entry = entry.with_expires_at(past)
    assert entry.is_expired()


def test_entry_version_increments() -> None:
    entry = _entry()
    versioned = entry.with_version(5)
    assert versioned.metadata.version == 5


def test_entry_with_redacted() -> None:
    entry = _entry()
    redacted_meta = entry.metadata.with_redacted()
    assert redacted_meta.redacted is True
    assert redacted_meta.classification == MemoryClassification.SENSITIVE


# ---------------------------------------------------------------------------
# WorkingMemory
# ---------------------------------------------------------------------------


def test_working_memory_put_and_get() -> None:
    wm = WorkingMemory()
    entry = _entry()
    wm.put(entry)
    retrieved = wm.get("k1", scope=MemoryScope.EXECUTION, scope_id="exec-1")
    assert retrieved is not None
    assert retrieved.key == "k1"


def test_working_memory_get_returns_none_for_missing() -> None:
    wm = WorkingMemory()
    assert wm.get("missing", scope=MemoryScope.EXECUTION, scope_id="exec-1") is None


def test_working_memory_remove() -> None:
    wm = WorkingMemory()
    wm.put(_entry())
    removed = wm.remove("k1", scope=MemoryScope.EXECUTION, scope_id="exec-1")
    assert removed is True
    assert wm.get("k1", scope=MemoryScope.EXECUTION, scope_id="exec-1") is None


def test_working_memory_evicts_expired() -> None:
    wm = WorkingMemory()
    past = datetime.now(UTC) - timedelta(seconds=1)
    entry = _entry().with_expires_at(past)
    wm.put(entry)
    assert wm.get("k1", scope=MemoryScope.EXECUTION, scope_id="exec-1") is None


def test_working_memory_evicts_fifo_at_capacity() -> None:
    wm = WorkingMemory(max_entries=2)
    e1 = _entry(key="a", value=1)
    e2 = _entry(key="b", value=2)
    e3 = _entry(key="c", value=3)
    wm.put(e1)
    wm.put(e2)
    wm.put(e3)
    assert wm.size == 2
    assert wm.get("a", scope=MemoryScope.EXECUTION, scope_id="exec-1") is None


def test_working_memory_clear_by_scope() -> None:
    wm = WorkingMemory()
    wm.put(_entry(key="a", scope_id="s1"))
    wm.put(_entry(key="b", scope_id="s1"))
    wm.put(_entry(key="c", scope_id="s2"))
    cleared = wm.clear(scope=MemoryScope.EXECUTION, scope_id="s1")
    assert cleared == 2
    assert wm.size == 1


def test_working_memory_list_entries() -> None:
    wm = WorkingMemory()
    wm.put(_entry(key="a"))
    wm.put(_entry(key="b"))
    entries = wm.list_entries(scope=MemoryScope.EXECUTION, scope_id="exec-1")
    assert len(entries) == 2


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_put_and_get() -> None:
    store = _store()
    entry = _entry()
    stored = await store.put(entry)
    assert stored.checksum is not None
    assert stored.metadata.version == 1

    retrieved = await store.get("k1", scope=MemoryScope.EXECUTION, scope_id="exec-1")
    assert retrieved is not None
    assert retrieved.key == "k1"


@pytest.mark.asyncio
async def test_store_put_increments_version_on_update() -> None:
    store = _store()
    entry = _entry()
    v1 = await store.put(entry)
    v2 = await store.put(v1)
    assert v2.metadata.version == 2


@pytest.mark.asyncio
async def test_store_get_returns_none_for_missing() -> None:
    store = _store()
    result = await store.get("nope", scope=MemoryScope.EXECUTION, scope_id="exec-1")
    assert result is None


@pytest.mark.asyncio
async def test_store_remove() -> None:
    store = _store()
    await store.put(_entry())
    removed = await store.remove("k1", scope=MemoryScope.EXECUTION, scope_id="exec-1")
    assert removed is True
    assert await store.get("k1", scope=MemoryScope.EXECUTION, scope_id="exec-1") is None


@pytest.mark.asyncio
async def test_store_exists() -> None:
    store = _store()
    assert not await store.exists("k1", scope=MemoryScope.EXECUTION, scope_id="exec-1")
    await store.put(_entry())
    assert await store.exists("k1", scope=MemoryScope.EXECUTION, scope_id="exec-1")


@pytest.mark.asyncio
async def test_store_search_by_pattern() -> None:
    store = _store()
    await store.put(_entry(key="alpha"))
    await store.put(_entry(key="beta"))
    results = await store.search(
        scope=MemoryScope.EXECUTION, scope_id="exec-1", pattern="alp"
    )
    assert len(results) == 1
    assert results[0].key == "alpha"


@pytest.mark.asyncio
async def test_store_search_by_tags() -> None:
    store = _store()
    await store.put(_entry(key="tagged", tags=("prod", "critical")))
    await store.put(_entry(key="plain"))
    results = await store.search(
        scope=MemoryScope.EXECUTION, scope_id="exec-1", tags=("prod",)
    )
    assert len(results) == 1
    assert results[0].key == "tagged"


@pytest.mark.asyncio
async def test_store_list_keys() -> None:
    store = _store()
    await store.put(_entry(key="x"))
    await store.put(_entry(key="y"))
    keys = await store.list_keys(scope=MemoryScope.EXECUTION, scope_id="exec-1")
    assert set(keys) == {"x", "y"}


# ---------------------------------------------------------------------------
# ShortTermMemory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_term_applies_default_ttl() -> None:
    provider = FakeMemoryProvider()
    store = MemoryStore(provider=provider, serializer=MemorySerializer())
    stm = ShortTermMemory(store=store, default_ttl_seconds=600.0)

    entry = _entry()  # no ttl
    stored = await stm.put(entry)
    assert stored.metadata.ttl_seconds == 600.0


@pytest.mark.asyncio
async def test_short_term_preserves_explicit_ttl() -> None:
    store = _store()
    stm = ShortTermMemory(store=store, default_ttl_seconds=600.0)
    entry = _entry(ttl_seconds=30.0)
    stored = await stm.put(entry)
    assert stored.metadata.ttl_seconds == 30.0


# ---------------------------------------------------------------------------
# SessionMemory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_memory_fixes_scope() -> None:
    store = _store()
    sm = SessionMemory(store=store)
    entry = _entry(scope=MemoryScope.EXECUTION, scope_id="should-be-overridden")
    stored = await sm.put(entry, session_id="sess-1")
    assert stored.metadata.scope == MemoryScope.SESSION
    assert stored.metadata.scope_id == "sess-1"


@pytest.mark.asyncio
async def test_session_memory_get_and_remove() -> None:
    store = _store()
    sm = SessionMemory(store=store)
    await sm.put(_entry(), session_id="sess-42")
    result = await sm.get("k1", session_id="sess-42")
    assert result is not None
    removed = await sm.remove("k1", session_id="sess-42")
    assert removed is True
    assert await sm.get("k1", session_id="sess-42") is None


# ---------------------------------------------------------------------------
# MemoryManager
# ---------------------------------------------------------------------------


def _manager() -> tuple[MemoryManager, FakeMemoryProvider, FakeMemoryProvider]:
    short_provider = FakeMemoryProvider()
    long_provider = FakeMemoryProvider()
    serializer = MemorySerializer()
    short_store = MemoryStore(provider=short_provider, serializer=serializer)
    long_store = MemoryStore(provider=long_provider, serializer=serializer)
    manager = MemoryManager(
        working=WorkingMemory(),
        short_term=ShortTermMemory(store=short_store),
        long_term=LongTermMemory(store=long_store),
    )
    return manager, short_provider, long_provider


@pytest.mark.asyncio
async def test_manager_routes_execution_scope_to_short_term() -> None:
    manager, short_provider, long_provider = _manager()
    entry = _entry(scope=MemoryScope.EXECUTION)
    await manager.put(entry)
    assert len(short_provider._data) == 1
    assert len(long_provider._data) == 0


@pytest.mark.asyncio
async def test_manager_routes_global_scope_to_long_term() -> None:
    manager, short_provider, long_provider = _manager()
    entry = _entry(scope=MemoryScope.GLOBAL, scope_id="g1")
    await manager.put(entry)
    assert len(short_provider._data) == 0
    assert len(long_provider._data) == 1


@pytest.mark.asyncio
async def test_manager_get_reads_from_working_first() -> None:
    manager, _, _ = _manager()
    entry = _entry()
    await manager.put(entry)
    # Second get should be served from working memory
    result = await manager.get("k1", scope=MemoryScope.EXECUTION, scope_id="exec-1")
    assert result is not None


@pytest.mark.asyncio
async def test_manager_promote_copies_to_long_term() -> None:
    manager, _, long_provider = _manager()
    entry = _entry(scope=MemoryScope.EXECUTION)
    await manager.put(entry)
    promoted = await manager.promote("k1", scope=MemoryScope.EXECUTION, scope_id="exec-1")
    assert promoted is not None
    assert len(long_provider._data) == 1


@pytest.mark.asyncio
async def test_manager_evict_working_calls_hook() -> None:
    hook = RecordingEvictionHook()
    short_store = _store()
    long_store = _store()
    manager = MemoryManager(
        working=WorkingMemory(),
        short_term=ShortTermMemory(store=short_store),
        long_term=LongTermMemory(store=long_store),
        eviction_hook=hook,
    )
    await manager.put(_entry(key="a"))
    await manager.put(_entry(key="b"))
    count = await manager.evict_working(scope=MemoryScope.EXECUTION, scope_id="exec-1")
    assert count == 2
    assert set(hook.evicted) == {"a", "b"}


@pytest.mark.asyncio
async def test_manager_summarize_returns_none_without_hook() -> None:
    manager, _, _ = _manager()
    await manager.put(_entry())
    result = await manager.summarize(scope=MemoryScope.EXECUTION, scope_id="exec-1")
    assert result is None


@pytest.mark.asyncio
async def test_manager_summarize_calls_hook() -> None:
    short_store = _store()
    long_store = _store()
    manager = MemoryManager(
        working=WorkingMemory(),
        short_term=ShortTermMemory(store=short_store),
        long_term=LongTermMemory(store=long_store),
        summarization_hook=PassthroughSummarizationHook(),
    )
    await manager.put(_entry(key="x"))
    await manager.put(_entry(key="y"))
    result = await manager.summarize(scope=MemoryScope.EXECUTION, scope_id="exec-1")
    assert result is not None
    assert result.key == "summary"
    assert result.value["count"] == 2


@pytest.mark.asyncio
async def test_manager_exists_checks_working_first() -> None:
    manager, _, _ = _manager()
    entry = _entry()
    await manager.put(entry)
    assert await manager.exists("k1", scope=MemoryScope.EXECUTION, scope_id="exec-1")
    assert not await manager.exists("missing", scope=MemoryScope.EXECUTION, scope_id="exec-1")
