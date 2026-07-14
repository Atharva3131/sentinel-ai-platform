"""Readiness checks for configured infrastructure dependencies."""

from __future__ import annotations

from azure.cosmos.aio import DatabaseProxy
from azure.storage.blob.aio import BlobServiceClient

from backend.infrastructure.neo4j import Neo4jConnection
from backend.infrastructure.redis import RedisConnection


class RedisHealthCheck:
    """Verify that Redis accepts commands."""

    name = "redis"

    def __init__(self, connection: RedisConnection) -> None:
        self._connection = connection

    async def check(self) -> None:
        if not await self._connection.ping():
            raise ConnectionError("Redis ping did not return success")


class Neo4jHealthCheck:
    """Verify Neo4j routing and connectivity."""

    name = "neo4j"

    def __init__(self, connection: Neo4jConnection) -> None:
        self._connection = connection

    async def check(self) -> None:
        await self._connection.verify_connectivity()


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
