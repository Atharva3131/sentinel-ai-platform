"""Redis client construction and connection wrappers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from redis.asyncio import Redis

from backend.configuration.settings import RedisSettings


def create_redis_client(settings: RedisSettings) -> Redis:
    """Create an async Redis client with production pool settings."""
    return cast(
        Redis,
        Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=settings.socket_timeout_seconds,
            socket_connect_timeout=settings.socket_connect_timeout_seconds,
            max_connections=settings.max_connections,
            health_check_interval=30,
        ),
    )


@dataclass(slots=True)
class RedisConnection:
    """Typed wrapper around the shared async Redis client."""

    client: Redis

    async def ping(self) -> bool:
        """Return True when the Redis server is reachable."""
        return cast(bool, await self.client.ping())

    async def close(self) -> None:
        """Close the underlying client gracefully."""
        close = getattr(self.client, "aclose", None) or getattr(self.client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    @property
    def connection_pool(self) -> Any:
        """Expose connection pool details for observability and testing."""
        return self.client.connection_pool
