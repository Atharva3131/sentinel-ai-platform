"""Cosmos DB infrastructure tests."""

from __future__ import annotations

from typing import Any, ClassVar, cast

import pytest
from pydantic import SecretStr

from backend.configuration.settings import CosmosSettings
from backend.db.retry import RetryPolicy
from backend.infrastructure.cosmos import (
    CosmosConnection,
    CosmosContainerFactory,
    CosmosPartitionStrategy,
    create_cosmos_client,
)
from backend.infrastructure.cosmos_repository import CosmosRepositoryBase
from backend.infrastructure.health_checks import CosmosHealthCheck


class FakeQueryResult:
    """Async iterable result set for Cosmos queries."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = list(items)

    def __aiter__(self) -> FakeQueryResult:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class FakeContainer:
    """Container double for repository tests."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.upserted: list[dict[str, Any]] = []
        self.reads: list[tuple[str, Any]] = []
        self.deletes: list[tuple[str, Any]] = []
        self.queries: list[tuple[str, Any, int | None]] = []
        self.items: dict[str, dict[str, Any]] = {}
        self.fail_upsert_once = False
        self.upsert_attempts = 0

    async def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        self.created.append(body)
        self.items[body["id"]] = body
        return body

    async def upsert_item(self, body: dict[str, Any]) -> dict[str, Any]:
        self.upsert_attempts += 1
        if self.fail_upsert_once and self.upsert_attempts == 1:
            raise ValueError("transient")
        self.upserted.append(body)
        self.items[body["id"]] = body
        return body

    async def read_item(self, item: str, partition_key: Any) -> dict[str, Any]:
        self.reads.append((item, partition_key))
        return self.items[item]

    async def delete_item(self, item: str, partition_key: Any) -> dict[str, Any]:
        self.deletes.append((item, partition_key))
        return self.items.pop(item)

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict[str, Any]] | None = None,
        partition_key: Any | None = None,
        max_item_count: int | None = None,
    ) -> FakeQueryResult:
        self.queries.append((query, partition_key, max_item_count))
        return FakeQueryResult(list(self.items.values()))


class FakeDatabase:
    """Database double for container factory tests."""

    def __init__(self, container: FakeContainer) -> None:
        self.container = container
        self.created: list[tuple[str, Any]] = []
        self.reads = 0

    async def read(self) -> dict[str, str]:
        self.reads += 1
        return {"id": "db"}

    def get_container_client(self, container_name: str) -> FakeContainer:
        self.created.append((container_name, None))
        return self.container

    async def create_container_if_not_exists(
        self,
        *,
        id: str,
        partition_key: Any,
    ) -> FakeContainer:
        self.created.append((id, partition_key))
        return self.container


class FakeClient:
    """Cosmos client double for connection and factory tests."""

    def __init__(self, container: FakeContainer) -> None:
        self.container = container
        self.database = FakeDatabase(container)
        self.closed = False
        self.database_requests: list[str] = []
        self.create_database_requests: list[str] = []

    def get_database_client(self, name: str) -> FakeDatabase:
        self.database_requests.append(name)
        return self.database

    async def create_database_if_not_exists(self, name: str) -> FakeDatabase:
        self.create_database_requests.append(name)
        return self.database

    async def aclose(self) -> None:
        self.closed = True


class RecorderClient:
    """Factory recorder used to validate client construction."""

    calls: ClassVar[list[tuple[str, tuple[Any, ...], dict[str, Any]]]] = []

    def __init__(self, endpoint: str | None = None, **kwargs: Any) -> None:
        self.endpoint = endpoint
        self.kwargs = kwargs

    @classmethod
    def from_connection_string(cls, connection_string: str, **kwargs: Any) -> RecorderClient:
        cls.calls.append(("from_connection_string", (connection_string,), kwargs))
        return cls(**kwargs)

    def __call__(self, endpoint: str, **kwargs: Any) -> RecorderClient:
        self.calls.append(("init", (endpoint,), kwargs))
        return self.__class__(endpoint, **kwargs)


@pytest.mark.asyncio
async def test_cosmos_client_factory_uses_connection_string_and_locations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Cosmos client factory should preserve connection configuration."""
    monkeypatch.setattr("backend.infrastructure.cosmos.CosmosClient", RecorderClient)
    settings = CosmosSettings(
        enabled=True,
        connection_string=SecretStr("AccountEndpoint=https://example;AccountKey=key"),
        preferred_locations=["East US", "West Europe"],
    )

    client = create_cosmos_client(settings)

    assert isinstance(client, RecorderClient)
    assert RecorderClient.calls[0][0] == "from_connection_string"
    assert RecorderClient.calls[0][1][0].startswith("AccountEndpoint=")
    assert RecorderClient.calls[0][2]["preferred_locations"] == ["East US", "West Europe"]


def test_cosmos_partition_strategy_extracts_nested_partition_keys() -> None:
    """Partition strategies should resolve nested document paths."""
    strategy = CosmosPartitionStrategy("/tenant/id")
    document = {"tenant": {"id": "tenant-1"}}

    assert strategy.normalized_path == "/tenant/id"
    assert strategy.segments == ("tenant", "id")
    assert strategy.value_from_document(document) == "tenant-1"
    assert strategy.container_partition_key().path == "/tenant/id"


@pytest.mark.asyncio
async def test_cosmos_container_factory_ensures_database_and_container() -> None:
    """Container factory should create lazy clients and can materialize resources."""
    container = FakeContainer()
    client = FakeClient(container)
    connection = CosmosConnection(cast(Any, client))
    strategy = CosmosPartitionStrategy("/tenantId")
    factory = CosmosContainerFactory(
        connection=connection,
        database_name="sentinel",
        container_name="documents",
        partition_strategy=strategy,
    )

    assert cast(Any, factory.database()) is client.database
    assert cast(Any, factory.container()) is container

    ensured_database = await factory.ensure_database()
    ensured_container = await factory.ensure_container()

    assert cast(Any, ensured_database) is client.database
    assert cast(Any, ensured_container) is container
    assert client.database_requests == ["sentinel", "sentinel"]
    assert client.create_database_requests == ["sentinel", "sentinel"]
    assert client.database.created[-1][0] == "documents"
    assert client.database.created[-1][1].path == "/tenantId"


@pytest.mark.asyncio
async def test_cosmos_repository_uses_partition_strategy_and_retries() -> None:
    """Repositories should stay storage-focused and retry transient failures."""
    container = FakeContainer()
    container.fail_upsert_once = True
    strategy = CosmosPartitionStrategy("/tenantId")
    policy_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        policy_delays.append(delay)

    retry_policy = RetryPolicy(
        max_attempts=3,
        initial_delay_seconds=0.01,
        max_delay_seconds=0.1,
        retryable_exceptions=(ValueError,),
        sleep=fake_sleep,
    )
    repository = CosmosRepositoryBase(
        container=cast(Any, container),
        partition_strategy=strategy,
        retry_policy=retry_policy,
    )

    document = {"id": "doc-1", "tenantId": "tenant-7", "payload": "value"}
    created = await repository.create(document)
    upserted = await repository.upsert(document)
    fetched = await repository.get("doc-1", "tenant-7")
    fetched_from_document = await repository.get_for_document("doc-1", document)
    deleted = await repository.delete("doc-1", "tenant-7")

    container.items["doc-2"] = {"id": "doc-2", "tenantId": "tenant-7", "payload": "value-2"}
    queried = await repository.query(
        "SELECT * FROM c",
        partition_key="tenant-7",
        max_item_count=10,
    )

    assert created == document
    assert upserted == document
    assert fetched == document
    assert fetched_from_document == document
    assert deleted == document
    assert queried == [container.items["doc-2"]]
    assert container.upsert_attempts == 2
    assert policy_delays == [0.01]
    assert container.reads[-1] == ("doc-1", "tenant-7")
    assert container.deletes[-1] == ("doc-1", "tenant-7")
    assert container.queries[-1] == ("SELECT * FROM c", "tenant-7", 10)


@pytest.mark.asyncio
async def test_cosmos_health_check_reads_database() -> None:
    """Cosmos readiness should delegate to the database client."""
    container = FakeContainer()
    database = FakeDatabase(container)
    check = CosmosHealthCheck(cast(Any, database))

    await check.check()

    assert database.reads == 1
