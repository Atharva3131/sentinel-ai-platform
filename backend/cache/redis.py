"""Redis-backed cache abstractions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from backend.infrastructure.redis import RedisConnection


@dataclass(slots=True)
class RedisCache:
    """Typed cache abstraction backed by Redis string keys."""

    connection: RedisConnection

    async def get(self, key: str) -> str | None:
        """Return the stored cache value or None when missing."""
        return cast(str | None, await self.connection.client.get(key))

    async def get_json(self, key: str) -> Any | None:
        """Return a JSON-decoded cache entry when present."""
        raw = await self.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def set(self, key: str, value: str, *, ttl_seconds: float | None = None) -> bool:
        """Store a string value with an optional time-to-live."""
        options: dict[str, Any] = {}
        if ttl_seconds is not None:
            options["ex"] = ttl_seconds
        return cast(bool, await self.connection.client.set(key, value, **options))

    async def set_json(self, key: str, value: Any, *, ttl_seconds: float | None = None) -> bool:
        """Store structured data as JSON."""
        return await self.set(key, json.dumps(value, sort_keys=True), ttl_seconds=ttl_seconds)

    async def delete(self, *keys: str) -> int:
        """Delete one or more cache entries."""
        return cast(int, await self.connection.client.delete(*keys))

    async def exists(self, *keys: str) -> bool:
        """Return True when at least one cache key exists."""
        return bool(cast(int, await self.connection.client.exists(*keys)))
