"""Neo4j infrastructure tests."""

from __future__ import annotations

from typing import Any, ClassVar, cast

import pytest
from neo4j.exceptions import ServiceUnavailable
from pydantic import SecretStr

from backend.configuration.settings import Neo4jSettings
from backend.db.retry import RetryPolicy
from backend.infrastructure.health_checks import Neo4jHealthCheck
from backend.infrastructure.neo4j import (
    Neo4jConnection,
    Neo4jCypherExecutor,
    create_neo4j_driver,
    create_neo4j_retry_policy,
)
from backend.infrastructure.neo4j_repository import Neo4jRepositoryBase


class FakeRecord:
    """Simple record double."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def data(self, *keys: Any) -> dict[str, Any]:
        return self.payload


class FakeResult:
    """Async result double."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = [FakeRecord(record) for record in records]

    async def data(self) -> list[dict[str, Any]]:
        return [record.data() for record in self.records]


class FakeSession:
    """Async Neo4j session double."""

    def __init__(self, driver: FakeDriver) -> None:
        self.driver = driver
        self.closed = False
        self.run_calls: list[tuple[str, dict[str, Any], str]] = []

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object | None,
    ) -> bool:
        self.closed = True
        return False

    async def run(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> FakeResult:
        params = dict(parameters or {})
        mode = self.driver.modes[-1] if self.driver.modes else "READ"
        self.run_calls.append((query, params, mode))
        if self.driver.failures_remaining > 0:
            self.driver.failures_remaining -= 1
            raise ServiceUnavailable("temporary")
        return FakeResult([{"query": query, "params": params, "mode": mode}])


class FakeDriver:
    """Async driver double."""

    def __init__(self, *, failures_remaining: int = 0) -> None:
        self.failures_remaining = failures_remaining
        self.sessions: list[FakeSession] = []
        self.verify_calls = 0
        self.closed = False
        self.modes: list[str] = []
        self.session_calls: list[dict[str, Any]] = []

    def session(self, *, database: str, default_access_mode: str, **kwargs: Any) -> FakeSession:
        self.session_calls.append(
            {"database": database, "default_access_mode": default_access_mode, **kwargs}
        )
        self.modes.append(default_access_mode)
        session = FakeSession(self)
        self.sessions.append(session)
        return session

    async def verify_connectivity(self, *, database: str) -> None:
        self.verify_calls += 1
        self.session_calls.append({"database": database, "default_access_mode": "VERIFY"})

    async def close(self) -> None:
        self.closed = True


class RecorderGraphDatabase:
    """Factory recorder for Neo4j driver construction."""

    calls: ClassVar[list[tuple[str, tuple[Any, ...], dict[str, Any]]]] = []

    @classmethod
    def driver(cls, uri: str, **kwargs: Any) -> Any:
        cls.calls.append(("driver", (uri,), kwargs))
        return object()


class FakeExecutor:
    """Repository delegate double."""

    def __init__(self) -> None:
        self.read_calls: list[tuple[str, dict[str, Any] | None, str | None]] = []
        self.write_calls: list[tuple[str, dict[str, Any] | None, str | None]] = []
        self.single_calls: list[tuple[str, dict[str, Any] | None, str | None]] = []

    async def read(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        self.read_calls.append((query, parameters, database))
        return [{"kind": "read"}]

    async def write(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        self.write_calls.append((query, parameters, database))
        return [{"kind": "write"}]

    async def single(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> dict[str, Any] | None:
        self.single_calls.append((query, parameters, database))
        return {"kind": "single"}


class SampleRepository(Neo4jRepositoryBase):
    """Test repository built on the Neo4j base."""


@pytest.mark.asyncio
async def test_neo4j_driver_factory_applies_pool_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver factory should preserve typed connection settings."""
    monkeypatch.setattr("backend.infrastructure.neo4j.AsyncGraphDatabase", RecorderGraphDatabase)
    settings = Neo4jSettings(
        enabled=True,
        host="graph.internal",
        port=7687,
        database="sentinel",
        username="neo4j_user",
        password=SecretStr("neo-pass"),
        connection_timeout_seconds=4.0,
        max_connection_pool_size=31,
    )

    driver = create_neo4j_driver(settings)

    assert isinstance(driver, object)
    assert RecorderGraphDatabase.calls[0][1][0] == "neo4j://graph.internal:7687"
    assert RecorderGraphDatabase.calls[0][2]["auth"] == ("neo4j_user", "neo-pass")
    assert RecorderGraphDatabase.calls[0][2]["connection_timeout"] == 4.0
    assert RecorderGraphDatabase.calls[0][2]["max_connection_pool_size"] == 31


@pytest.mark.asyncio
async def test_neo4j_connection_verifies_and_closes_driver() -> None:
    """Connection wrappers should delegate verification and closing."""
    driver = FakeDriver()
    connection = Neo4jConnection(driver=cast(Any, driver), database="sentinel")

    await connection.verify_connectivity()
    await connection.close()

    assert driver.verify_calls == 1
    assert driver.closed is True
    assert driver.session_calls[0]["database"] == "sentinel"


@pytest.mark.asyncio
async def test_cypher_executor_retries_transient_failures() -> None:
    """Cypher execution should retry transient driver failures."""
    driver = FakeDriver(failures_remaining=1)
    connection = Neo4jConnection(driver=cast(Any, driver), database="sentinel")
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    policy = RetryPolicy(
        max_attempts=3,
        initial_delay_seconds=0.01,
        max_delay_seconds=0.1,
        retryable_exceptions=(ServiceUnavailable,),
        sleep=fake_sleep,
    )
    executor = Neo4jCypherExecutor(connection=connection, retry_policy=policy)

    records = await executor.read("MATCH (n) RETURN n", {"limit": 1})

    assert records == [{"query": "MATCH (n) RETURN n", "params": {"limit": 1}, "mode": "READ"}]
    assert delays == [0.01]
    assert len(driver.sessions) == 2
    assert driver.sessions[0].run_calls[0][0] == "MATCH (n) RETURN n"
    assert driver.sessions[1].closed is True


@pytest.mark.asyncio
async def test_neo4j_repository_delegates_to_executor() -> None:
    """Repository helpers should stay thin over the Cypher executor."""
    executor = FakeExecutor()
    repository = SampleRepository(executor=cast(Any, executor))

    read_rows = await repository.read("MATCH (n) RETURN n")
    write_rows = await repository.write("CREATE (n)")
    single_row = await repository.single("MATCH (n) RETURN n LIMIT 1")

    assert read_rows == [{"kind": "read"}]
    assert write_rows == [{"kind": "write"}]
    assert single_row == {"kind": "single"}
    assert executor.read_calls[0][0] == "MATCH (n) RETURN n"
    assert executor.write_calls[0][0] == "CREATE (n)"
    assert executor.single_calls[0][0] == "MATCH (n) RETURN n LIMIT 1"


@pytest.mark.asyncio
async def test_neo4j_health_check_uses_connection() -> None:
    """Neo4j readiness should delegate to the connection wrapper."""
    driver = FakeDriver()
    connection = Neo4jConnection(driver=cast(Any, driver), database="sentinel")
    check = Neo4jHealthCheck(connection)

    await check.check()

    assert driver.verify_calls == 1


def test_neo4j_retry_policy_covers_transient_driver_errors() -> None:
    """Neo4j retry policy should target transient infrastructure exceptions."""
    policy = create_neo4j_retry_policy()

    assert ServiceUnavailable in policy.retryable_exceptions
