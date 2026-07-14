"""Readiness checks for configured infrastructure dependencies."""

from __future__ import annotations

from azure.cosmos.aio import DatabaseProxy
from azure.storage.blob.aio import BlobServiceClient
from neo4j import AsyncDriver
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class PostgresHealthCheck:
    """Verify that PostgreSQL accepts a minimal query."""

    name = "postgresql"

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))


class RedisHealthCheck:
    """Verify that Redis accepts commands."""

    name = "redis"

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def check(self) -> None:
        if not await self._client.ping():
            raise ConnectionError("Redis ping did not return success")


class Neo4jHealthCheck:
    """Verify Neo4j routing and connectivity."""

    name = "neo4j"

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver

    async def check(self) -> None:
        await self._driver.verify_connectivity()


class CosmosHealthCheck:
    """Verify access to the configured Cosmos DB database."""

    name = "cosmos_db"

    def __init__(self, database: DatabaseProxy) -> None:
        self._database = database

    async def check(self) -> None:
        await self._database.read()


class BlobHealthCheck:
    """Verify access to the configured Blob Storage account."""

    name = "blob_storage"

    def __init__(self, client: BlobServiceClient) -> None:
        self._client = client

    async def check(self) -> None:
        await self._client.get_account_information()
