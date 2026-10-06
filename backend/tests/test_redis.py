"""Redis infrastructure tests."""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from backend.cache.redis import RedisCache
from backend.configuration.settings import RedisSettings
from backend.db.retry import RetryPolicy
from backend.infrastructure.health_checks import RedisHealthCheck
from backend.infrastructure.redis import RedisConnection, create_redis_client
from backend.queues.redis import (
    RedisDeadLetterQueue,
    RedisDistributedLock,
    RedisLockManager,
    RedisPubSub,
    RedisRetryQueue,
    RedisStreamClient,
    RedisStreamRecord,
)


class FakePubSub:
    """Test double for Redis pub/sub subscriptions."""

    def __init__(self) -> None:
        self.subscribed: list[tuple[str, ...]] = []
        self.unsubscribed: list[tuple[str, ...]] = []
        self.closed = False

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.append(channels)

    async def unsubscribe(self, *channels: str) -> None:
        self.unsubscribed.append(channels)

    async def close(self) -> None:
        self.closed = True


class FakeLock:
    """Test double for redis lock objects."""

    def __init__(self, acquired: bool = True) -> None:
        self.acquire_calls: list[dict[str, Any]] = []
        self.release_calls = 0
        self._acquired = acquired

    async def acquire(self, **kwargs: Any) -> bool:
        self.acquire_calls.append(kwargs)
        return self._acquired

    async def release(self) -> None:
        self.release_calls += 1


class FakeRedis:
    """Test double for the async Redis client."""

    def __init__(
        self,
        *,
        ping_results: list[bool] | None = None,
        xread_response: list[tuple[str, list[tuple[str, dict[str, Any]]]]] | None = None,
        xreadgroup_response: list[tuple[str, list[tuple[str, dict[str, Any]]]]] | None = None,
    ) -> None:
        self.storage: dict[str, str] = {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.ping_results = ping_results or [True]
        self.xread_response = xread_response or []
        self.xreadgroup_response = xreadgroup_response or []
        self.pubsub_obj = FakePubSub()
        self.lock_obj = FakeLock()
        self.closed = False
        self.connection_pool = type("Pool", (), {"max_connections": 64})()

    async def ping(self) -> bool:
        self.calls.append(("ping", (), {}))
        return self.ping_results.pop(0)

    async def get(self, key: str) -> str | None:
        self.calls.append(("get", (key,), {}))
        return self.storage.get(key)

    async def set(self, key: str, value: str, **kwargs: Any) -> bool:
        self.calls.append(("set", (key, value), kwargs))
        self.storage[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        self.calls.append(("delete", keys, {}))
        removed = 0
        for key in keys:
            if key in self.storage:
                removed += 1
                del self.storage[key]
        return removed

    async def exists(self, *keys: str) -> int:
        self.calls.append(("exists", keys, {}))
        return sum(1 for key in keys if key in self.storage)

    async def xadd(self, stream: str, fields: dict[str, Any], *, id: str) -> str:
        self.calls.append(("xadd", (stream, fields), {"id": id}))
        return "1-0"

    async def xread(
        self,
        *,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, Any]]]]]:
        self.calls.append(("xread", (streams,), {"count": count, "block": block}))
        return self.xread_response

    async def xreadgroup(
        self,
        *,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, Any]]]]]:
        self.calls.append(
            (
                "xreadgroup",
                (groupname, consumername, streams),
                {"count": count, "block": block},
            )
        )
        return self.xreadgroup_response

    async def xgroup_create(
        self,
        *,
        name: str,
        groupname: str,
        id: str,
        mkstream: bool,
    ) -> None:
        self.calls.append(("xgroup_create", (name, groupname), {"id": id, "mkstream": mkstream}))

    async def xack(self, stream: str, group: str, *message_ids: str) -> int:
        self.calls.append(("xack", (stream, group, *message_ids), {}))
        return len(message_ids)

    async def xtrim(self, stream: str, *, maxlen: int) -> int:
        self.calls.append(("xtrim", (stream,), {"maxlen": maxlen}))
        return maxlen

    async def publish(self, channel: str, message: str) -> int:
        self.calls.append(("publish", (channel, message), {}))
        return 1

    def pubsub(self) -> FakePubSub:
        self.calls.append(("pubsub", (), {}))
        return self.pubsub_obj

    def lock(
        self,
        name: str,
        *,
        timeout: float | None = None,
        blocking: bool = True,
        blocking_timeout: float | None = None,
    ) -> FakeLock:
        self.calls.append(
            (
                "lock",
                (name,),
                {
                    "timeout": timeout,
                    "blocking": blocking,
                    "blocking_timeout": blocking_timeout,
                },
            )
        )
        return self.lock_obj

    async def aclose(self) -> None:
        self.closed = True


def _connection(fake: FakeRedis) -> RedisConnection:
    return RedisConnection(cast(Redis, fake))


@pytest.mark.asyncio
async def test_redis_health_check_retries_ping() -> None:
    """Redis readiness should retry transient ping failures."""
    fake = FakeRedis(ping_results=[False, True])
    
    async def fake_sleep(_: float) -> None:
        return None

    policy = RetryPolicy(
        max_attempts=3,
        initial_delay_seconds=0.01,
        max_delay_seconds=0.1,
        retryable_exceptions=(ConnectionError,),
        sleep=fake_sleep,
    )
    check = RedisHealthCheck(_connection(fake))

    attempts = 0

    async def runner() -> None:
        nonlocal attempts
        attempts += 1
        await check.check()

    await policy.run(runner)

    assert attempts == 2
    assert fake.calls[0][0] == "ping"


@pytest.mark.asyncio
async def test_cache_abstraction_delegates_to_client() -> None:
    """Cache operations should delegate to the underlying Redis client."""
    fake = FakeRedis()
    cache = RedisCache(_connection(fake))

    assert await cache.set("cache:key", "value", ttl_seconds=30) is True
    assert await cache.get("cache:key") == "value"
    assert await cache.set_json("cache:json", {"answer": 42}) is True
    assert await cache.get_json("cache:json") == {"answer": 42}
    assert await cache.exists("cache:key", "cache:json") is True
    assert await cache.delete("cache:key", "cache:json") == 2

    assert [call[0] for call in fake.calls[:4]] == ["set", "get", "set", "get"]


@pytest.mark.asyncio
async def test_stream_pubsub_and_lock_wrappers_delegate() -> None:
    """Stream, pub/sub, and lock wrappers should stay thin over the Redis client."""
    fake = FakeRedis(
        xread_response=[("stream:a", [("1-0", {"payload": "one"})])],
        xreadgroup_response=[("stream:a", [("2-0", {"payload": "two"})])],
    )
    connection = _connection(fake)
    streams = RedisStreamClient(connection)

    message_id = await streams.add("stream:a", {"payload": "value"})
    records = await streams.read({"stream:a": "0-0"})
    grouped = await streams.read(
        {"stream:a": ">"},
        group="group-a",
        consumer="consumer-a",
    )
    await streams.create_consumer_group("stream:a", "group-a")
    acked = await streams.ack("stream:a", "group-a", "1-0", "2-0")
    trimmed = await streams.trim("stream:a", maxlen=100)

    pubsub = RedisPubSub(connection)
    async with pubsub.subscription("events") as subscription:
        assert subscription is fake.pubsub_obj
    published = await pubsub.publish("events", "payload")

    lock_manager = RedisLockManager(connection)
    async with lock_manager.lock(
        "resource-a",
        timeout_seconds=5.0,
        blocking_timeout_seconds=1.0,
    ) as lock:
        assert isinstance(lock, RedisDistributedLock)
    assert fake.lock_obj.release_calls == 1
    lock_call = next(call for call in fake.calls if call[0] == "lock")
    assert lock_call[2]["timeout"] == 5.0
    assert lock_call[2]["blocking_timeout"] == 1.0

    assert message_id == "1-0"
    assert records == [RedisStreamRecord("stream:a", "1-0", {"payload": "one"})]
    assert grouped == [RedisStreamRecord("stream:a", "2-0", {"payload": "two"})]
    assert acked == 2
    assert trimmed == 100
    assert published == 1
    assert fake.calls[1][0] == "xread"
    assert fake.calls[2][0] == "xreadgroup"
    assert fake.calls[3][0] == "xgroup_create"


@pytest.mark.asyncio
async def test_retry_and_dead_letter_queues_use_distinct_stream_names() -> None:
    """Retry and dead-letter queues should target different stream names."""
    fake = FakeRedis()
    streams = RedisStreamClient(_connection(fake))
    retry_queue = RedisRetryQueue(streams)
    dead_letter_queue = RedisDeadLetterQueue(streams)

    retry_message = await retry_queue.enqueue({"reason": "timeout"})
    dead_letter_message = await dead_letter_queue.enqueue({"reason": "poison"})

    assert retry_message == "1-0"
    assert dead_letter_message == "1-0"
    assert fake.calls[0][1][0] == "sentinel:queue:retry"
    assert fake.calls[1][1][0] == "sentinel:queue:dead-letter"


def test_redis_client_factory_applies_pooling_settings() -> None:
    """The Redis client factory should translate typed settings into pool options."""
    settings = RedisSettings(
        host="cache.internal",
        port=6380,
        database=5,
        username=None,
        password=SecretStr("redis-pass"),
        ssl=False,
        socket_timeout_seconds=4.0,
        socket_connect_timeout_seconds=1.5,
        max_connections=42,
    )

    client = create_redis_client(settings)
    pool = cast(Any, client.connection_pool)

    assert pool.max_connections == 42
    assert pool.connection_kwargs["decode_responses"] is True
    assert pool.connection_kwargs["socket_timeout"] == 4.0
    assert pool.connection_kwargs["socket_connect_timeout"] == 1.5
    assert pool.connection_kwargs["db"] == 5
    assert pool.connection_kwargs["host"] == "cache.internal"
    assert pool.connection_kwargs["port"] == 6380
