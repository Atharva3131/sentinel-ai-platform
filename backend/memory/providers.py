"""Memory provider Protocol and backend implementations (Redis, Cosmos DB)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from backend.infrastructure.cosmos_repository import CosmosRepositoryBase
from backend.infrastructure.redis import RedisConnection
from backend.memory.exceptions import MemoryStorageError
from backend.memory.serializer import MemorySerializer

_REDIS_NS = "sentinel:memory"


@runtime_checkable
class MemoryProvider(Protocol):
    """Storage-provider contract for memory backends.

    Implementations must be async-safe and idempotent on write.
    The `scope` argument is a composite string of the form ``{scope_type}:{scope_id}``.
    """

    async def read(self, key: str, *, scope: str) -> dict[str, Any] | None: ...

    async def write(
        self,
        key: str,
        data: dict[str, Any],
        *,
        scope: str,
        ttl_seconds: float | None = None,
    ) -> None: ...

    async def delete(self, key: str, *, scope: str) -> bool: ...

    async def exists(self, key: str, *, scope: str) -> bool: ...

    async def search(
        self,
        *,
        scope: str,
        pattern: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    async def list_keys(self, *, scope: str, limit: int = 100) -> list[str]: ...


@dataclass(slots=True)
class RedisMemoryProvider:
    """Redis-backed memory provider using string keys and JSON values.

    Key layout: ``sentinel:memory:{scope}:{key}``
    Search uses SCAN so it is safe for production key cardinalities.
    """

    connection: RedisConnection
    serializer: MemorySerializer
    namespace: str = _REDIS_NS

    def _redis_key(self, key: str, scope: str) -> str:
        return f"{self.namespace}:{scope}:{key}"

    def _scope_prefix(self, scope: str) -> str:
        return f"{self.namespace}:{scope}:"

    def _strip_prefix(self, full_key: str, scope: str) -> str:
        prefix = self._scope_prefix(scope)
        return full_key[len(prefix):] if full_key.startswith(prefix) else full_key

    async def read(self, key: str, *, scope: str) -> dict[str, Any] | None:
        try:
            raw = cast(str | None, await self.connection.client.get(self._redis_key(key, scope)))
            if raw is None:
                return None
            return cast(dict[str, Any], json.loads(raw))
        except Exception as exc:
            raise MemoryStorageError(f"Redis read failed for key '{key}'") from exc

    async def write(
        self,
        key: str,
        data: dict[str, Any],
        *,
        scope: str,
        ttl_seconds: float | None = None,
    ) -> None:
        try:
            payload = json.dumps(data, sort_keys=True, default=str)
            redis_key = self._redis_key(key, scope)
            if ttl_seconds is not None:
                await self.connection.client.set(redis_key, payload, ex=int(ttl_seconds))
            else:
                await self.connection.client.set(redis_key, payload)
        except Exception as exc:
            raise MemoryStorageError(f"Redis write failed for key '{key}'") from exc

    async def delete(self, key: str, *, scope: str) -> bool:
        try:
            count = cast(int, await self.connection.client.delete(self._redis_key(key, scope)))
            return count > 0
        except Exception as exc:
            raise MemoryStorageError(f"Redis delete failed for key '{key}'") from exc

    async def exists(self, key: str, *, scope: str) -> bool:
        try:
            return bool(cast(int, await self.connection.client.exists(self._redis_key(key, scope))))
        except Exception as exc:
            raise MemoryStorageError(f"Redis exists check failed for key '{key}'") from exc

    async def search(
        self,
        *,
        scope: str,
        pattern: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        try:
            prefix = self._scope_prefix(scope)
            results: list[dict[str, Any]] = []
            async for redis_key in self.connection.client.scan_iter(match=f"{prefix}*", count=200):
                if len(results) >= limit:
                    break
                raw = cast(str | None, await self.connection.client.get(redis_key))
                if raw is None:
                    continue
                try:
                    data = cast(dict[str, Any], json.loads(raw))
                except json.JSONDecodeError:
                    continue
                entry_key = str(data.get("key", ""))
                if pattern and pattern not in entry_key:
                    continue
                if tags:
                    entry_tags = set(data.get("metadata", {}).get("tags", []))
                    if not all(t in entry_tags for t in tags):
                        continue
                results.append(data)
            return results
        except MemoryStorageError:
            raise
        except Exception as exc:
            raise MemoryStorageError("Redis search failed") from exc

    async def list_keys(self, *, scope: str, limit: int = 100) -> list[str]:
        try:
            prefix = self._scope_prefix(scope)
            keys: list[str] = []
            async for redis_key in self.connection.client.scan_iter(match=f"{prefix}*", count=200):
                if len(keys) >= limit:
                    break
                keys.append(self._strip_prefix(str(redis_key), scope))
            return keys
        except Exception as exc:
            raise MemoryStorageError("Redis list_keys failed") from exc


@dataclass(slots=True)
class CosmosMemoryProvider:
    """Cosmos DB-backed memory provider.

    Document structure::

        {
            "id": "{scope}:{key}",
            "partitionKey": "{scope}",
            "key": "{key}",
            "scope_composite": "{scope}",
            "tags": [...],
            "entry": { ...serialized MemoryEntry... }
        }

    Cosmos TTL is set via the ``ttl`` field when ``ttl_seconds`` is provided.
    """

    repository: CosmosRepositoryBase
    serializer: MemorySerializer

    def _doc_id(self, key: str, scope: str) -> str:
        return f"{scope}:{key}"

    async def read(self, key: str, *, scope: str) -> dict[str, Any] | None:
        try:
            doc = await self.repository.get(self._doc_id(key, scope), partition_key=scope)
            if doc is None:
                return None
            return cast(dict[str, Any], doc.get("entry"))
        except Exception as exc:
            raise MemoryStorageError(f"Cosmos read failed for key '{key}'") from exc

    async def write(
        self,
        key: str,
        data: dict[str, Any],
        *,
        scope: str,
        ttl_seconds: float | None = None,
    ) -> None:
        try:
            doc: dict[str, Any] = {
                "id": self._doc_id(key, scope),
                "partitionKey": scope,
                "key": key,
                "scope_composite": scope,
                "tags": data.get("metadata", {}).get("tags", []),
                "entry": data,
            }
            if ttl_seconds is not None:
                doc["ttl"] = int(ttl_seconds)
            await self.repository.upsert(doc)
        except Exception as exc:
            raise MemoryStorageError(f"Cosmos write failed for key '{key}'") from exc

    async def delete(self, key: str, *, scope: str) -> bool:
        try:
            result = await self.repository.delete(self._doc_id(key, scope), partition_key=scope)
            return result is not None
        except Exception as exc:
            raise MemoryStorageError(f"Cosmos delete failed for key '{key}'") from exc

    async def exists(self, key: str, *, scope: str) -> bool:
        return await self.read(key, scope=scope) is not None

    async def search(
        self,
        *,
        scope: str,
        pattern: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        try:
            conditions = ["c.scope_composite = @scope"]
            parameters: list[dict[str, Any]] = [{"name": "@scope", "value": scope}]
            if pattern:
                conditions.append("CONTAINS(c.key, @pattern)")
                parameters.append({"name": "@pattern", "value": pattern})
            for i, tag in enumerate(tags):
                param_name = f"@tag{i}"
                conditions.append(f"ARRAY_CONTAINS(c.tags, {param_name})")
                parameters.append({"name": param_name, "value": tag})
            query = "SELECT * FROM c WHERE " + " AND ".join(conditions)
            docs = await self.repository.query(
                query,
                partition_key=scope,
                parameters=parameters,
                max_item_count=limit,
            )
            return [cast(dict[str, Any], doc.get("entry")) for doc in docs if doc.get("entry")]
        except MemoryStorageError:
            raise
        except Exception as exc:
            raise MemoryStorageError("Cosmos search failed") from exc

    async def list_keys(self, *, scope: str, limit: int = 100) -> list[str]:
        try:
            docs = await self.repository.query(
                "SELECT c.key FROM c WHERE c.scope_composite = @scope",
                partition_key=scope,
                parameters=[{"name": "@scope", "value": scope}],
                max_item_count=limit,
            )
            return [str(doc["key"]) for doc in docs if "key" in doc]
        except Exception as exc:
            raise MemoryStorageError("Cosmos list_keys failed") from exc
