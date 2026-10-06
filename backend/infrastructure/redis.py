"""Redis client construction and connection wrappers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from redis.asyncio import Redis
from redis_entraid.cred_provider import (  # type: ignore[import-untyped]
    create_from_default_azure_credential,
)

from backend.configuration.settings import RedisSettings

AZURE_REDIS_SCOPE = "https://redis.azure.com/.default"


def create_redis_client(
    settings: RedisSettings,
    *,
    managed_identity_client_id: str | None = None,
) -> Redis:
    """Create an async Redis client with production pool settings."""

    if settings.ssl and settings.username:
        if not managed_identity_client_id:
            raise ValueError(
                "Azure Redis Entra authentication requires a managed "
                "identity client ID."
            )

        credential_provider = create_from_default_azure_credential(
            scopes=(AZURE_REDIS_SCOPE,),
            app_kwargs={
                "managed_identity_client_id": managed_identity_client_id,
            },
        )

        redis_url = (
            f"rediss://{settings.host}:{settings.port}/{settings.database}"
        )

        return cast(
            Redis,
            Redis.from_url(
                redis_url,
                credential_provider=credential_provider,
                decode_responses=True,
                socket_timeout=settings.socket_timeout_seconds,
                socket_connect_timeout=settings.socket_connect_timeout_seconds,
                max_connections=settings.max_connections,
                health_check_interval=30,
            ),
        )

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
        close = getattr(self.client, "aclose", None) or getattr(
            self.client,
            "close",
            None,
        )
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    @property
    def connection_pool(self) -> Any:
        """Expose connection pool details for observability and testing."""
        return self.client.connection_pool