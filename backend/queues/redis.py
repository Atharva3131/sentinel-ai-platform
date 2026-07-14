"""Redis stream, pub/sub, and lock abstractions."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from backend.infrastructure.redis import RedisConnection


@dataclass(frozen=True, slots=True)
class RedisStreamRecord:
    """Normalized Redis stream entry."""

    stream: str
    message_id: str
    payload: dict[str, Any]


def _normalize_stream_response(
    response: Any,
) -> list[RedisStreamRecord]:
    records: list[RedisStreamRecord] = []
    for stream, entries in response or []:
        for message_id, payload in entries:
            records.append(
                RedisStreamRecord(
                    stream=stream,
                    message_id=message_id,
                    payload=dict(payload),
                )
            )
    return records


@dataclass(slots=True)
class RedisStreamClient:
    """Typed Redis Streams access layer."""

    connection: RedisConnection

    async def add(
        self,
        stream: str,
        fields: Mapping[str, Any],
        *,
        message_id: str = "*",
    ) -> str:
        """Append a message to a Redis stream."""
        return cast(
            str,
            await self.connection.client.xadd(
                stream,
                cast(Any, dict(fields)),
                id=message_id,
            ),
        )

    async def read(
        self,
        streams: Mapping[str, str],
        *,
        count: int | None = None,
        block_ms: int | None = None,
        group: str | None = None,
        consumer: str | None = None,
    ) -> list[RedisStreamRecord]:
        """Read messages from one or more streams."""
        if (group is None) ^ (consumer is None):
            raise ValueError("group and consumer must be provided together")
        if group is not None and consumer is not None:
            response = cast(
                Any,
                await self.connection.client.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams=cast(Any, dict(streams)),
                    count=count,
                    block=block_ms,
                ),
            )
            return _normalize_stream_response(response)
        response = cast(
            Any,
            await self.connection.client.xread(
                streams=cast(Any, dict(streams)),
                count=count,
                block=block_ms,
            ),
        )
        return _normalize_stream_response(response)

    async def create_consumer_group(
        self,
        stream: str,
        group: str,
        *,
        start_id: str = "0-0",
        mkstream: bool = True,
    ) -> None:
        """Create a consumer group if it does not already exist."""
        await self.connection.client.xgroup_create(
            name=stream,
            groupname=group,
            id=start_id,
            mkstream=mkstream,
        )

    async def ack(self, stream: str, group: str, *message_ids: str) -> int:
        """Acknowledge stream entries after successful processing."""
        return cast(int, await self.connection.client.xack(stream, group, *message_ids))

    async def trim(self, stream: str, *, maxlen: int) -> int:
        """Trim a stream to a bounded size."""
        return cast(int, await self.connection.client.xtrim(stream, maxlen=maxlen))


@dataclass(slots=True)
class RedisPubSub:
    """Typed Redis Pub/Sub abstraction."""

    connection: RedisConnection

    async def publish(self, channel: str, message: str) -> int:
        """Publish a message to a channel."""
        return cast(int, await self.connection.client.publish(channel, message))

    @asynccontextmanager
    async def subscription(self, *channels: str) -> AsyncIterator[Any]:
        """Subscribe to one or more channels and auto-clean up the subscription."""
        pubsub = self.connection.client.pubsub()
        await pubsub.subscribe(*channels)
        try:
            yield pubsub
        finally:
            await pubsub.unsubscribe(*channels)
            close = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result


@dataclass(slots=True)
class RedisDistributedLock:
    """Concrete Redis lock wrapper."""

    lock: Any
    acquired: bool = False

    async def acquire(self, **kwargs: Any) -> bool:
        """Acquire the lock."""
        self.acquired = cast(bool, await self.lock.acquire(**kwargs))
        return self.acquired

    async def release(self) -> None:
        """Release the lock when currently held."""
        if self.acquired:
            await self.lock.release()
            self.acquired = False

    async def __aenter__(self) -> RedisDistributedLock:
        if not await self.acquire():
            raise TimeoutError("Redis lock acquisition timed out")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object | None,
    ) -> bool:
        await self.release()
        return False


@dataclass(slots=True)
class RedisLockManager:
    """Factory for named Redis distributed locks."""

    connection: RedisConnection

    def lock(
        self,
        name: str,
        *,
        timeout_seconds: float | None = None,
        blocking: bool = True,
        blocking_timeout_seconds: float | None = None,
    ) -> RedisDistributedLock:
        """Create a named distributed lock."""
        lock = self.connection.client.lock(
            name,
            timeout=timeout_seconds,
            blocking=blocking,
            blocking_timeout=blocking_timeout_seconds,
        )
        return RedisDistributedLock(lock)


@dataclass(slots=True)
class RedisStreamQueue:
    """Convenience wrapper for a single Redis stream."""

    streams: RedisStreamClient
    stream_name: str

    async def enqueue(
        self,
        fields: Mapping[str, Any],
        *,
        message_id: str = "*",
    ) -> str:
        """Append a queue item to the stream."""
        return await self.streams.add(self.stream_name, fields, message_id=message_id)

    async def read(
        self,
        *,
        count: int | None = None,
        block_ms: int | None = None,
        group: str | None = None,
        consumer: str | None = None,
    ) -> list[RedisStreamRecord]:
        """Read queue items from the stream."""
        stream_id = ">" if group is not None else "0-0"
        return await self.streams.read(
            {self.stream_name: stream_id},
            count=count,
            block_ms=block_ms,
            group=group,
            consumer=consumer,
        )

    async def ack(self, group: str, *message_ids: str) -> int:
        """Acknowledge processed queue items."""
        return await self.streams.ack(self.stream_name, group, *message_ids)

    async def create_consumer_group(
        self,
        group: str,
        *,
        start_id: str = "0-0",
        mkstream: bool = True,
    ) -> None:
        """Create the queue consumer group."""
        await self.streams.create_consumer_group(
            self.stream_name,
            group,
            start_id=start_id,
            mkstream=mkstream,
        )


@dataclass(slots=True)
class RedisRetryQueue(RedisStreamQueue):
    """Retry queue backed by a dedicated Redis stream."""

    stream_name: str = "sentinel:queue:retry"


@dataclass(slots=True)
class RedisDeadLetterQueue(RedisStreamQueue):
    """Dead-letter queue backed by a dedicated Redis stream."""

    stream_name: str = "sentinel:queue:dead-letter"
