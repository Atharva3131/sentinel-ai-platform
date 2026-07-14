"""Application dependency container and resource lifecycle."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

from azure.cosmos.aio import CosmosClient, DatabaseProxy
from azure.identity.aio import ClientSecretCredential, DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient
from neo4j import AsyncDriver, AsyncGraphDatabase
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from backend.application.health import HealthService
from backend.configuration.settings import AppSettings
from backend.infrastructure.health_checks import (
    BlobHealthCheck,
    CosmosHealthCheck,
    Neo4jHealthCheck,
    PostgresHealthCheck,
    RedisHealthCheck,
)
from backend.interfaces.health import HealthCheck


@dataclass(slots=True)
class ApplicationContainer:
    """Own application dependencies and close them in a deterministic order."""

    settings: AppSettings
    sqlalchemy_engine: AsyncEngine
    redis: Redis
    health_service: HealthService
    neo4j: AsyncDriver | None = None
    cosmos: CosmosClient | None = None
    cosmos_database: DatabaseProxy | None = None
    blob: BlobServiceClient | None = None
    azure_credentials: list[DefaultAzureCredential | ClientSecretCredential] = field(
        default_factory=list
    )

    @classmethod
    def build(cls, settings: AppSettings) -> ApplicationContainer:
        """Construct lazy clients without performing network I/O."""
        engine = create_async_engine(
            settings.postgres.sqlalchemy_url,
            pool_pre_ping=True,
            pool_size=settings.postgres.pool_size,
            max_overflow=settings.postgres.max_overflow,
            pool_timeout=settings.postgres.pool_timeout_seconds,
            pool_recycle=settings.postgres.pool_recycle_seconds,
        )
        redis_client = Redis.from_url(
            settings.redis.redis_url,
            decode_responses=True,
            socket_timeout=settings.redis.socket_timeout_seconds,
            socket_connect_timeout=settings.redis.socket_connect_timeout_seconds,
            max_connections=settings.redis.max_connections,
            health_check_interval=30,
        )

        checks: list[HealthCheck] = [
            PostgresHealthCheck(engine),
            RedisHealthCheck(redis_client),
        ]
        neo4j_driver: AsyncDriver | None = None
        cosmos_client: CosmosClient | None = None
        cosmos_database: DatabaseProxy | None = None
        blob_client: BlobServiceClient | None = None
        credentials: list[DefaultAzureCredential | ClientSecretCredential] = []

        if settings.neo4j.enabled:
            neo4j_driver = AsyncGraphDatabase.driver(
                settings.neo4j.driver_uri,
                auth=(settings.neo4j.username, settings.neo4j.password.get_secret_value()),
                connection_timeout=settings.neo4j.connection_timeout_seconds,
                max_connection_pool_size=settings.neo4j.max_connection_pool_size,
            )
            checks.append(Neo4jHealthCheck(neo4j_driver))

        if settings.cosmos.enabled:
            if settings.cosmos.connection_string:
                cosmos_client = CosmosClient.from_connection_string(
                    settings.cosmos.connection_string.get_secret_value()
                )
            else:
                credential = _build_azure_credential(settings)
                credentials.append(credential)
                cosmos_client = CosmosClient(settings.cosmos.endpoint or "", credential=credential)
            cosmos_database = cosmos_client.get_database_client(settings.cosmos.database_name)
            checks.append(CosmosHealthCheck(cosmos_database))

        if settings.blob.enabled:
            if settings.blob.connection_string:
                blob_client = BlobServiceClient.from_connection_string(
                    settings.blob.connection_string.get_secret_value()
                )
            else:
                credential = _build_azure_credential(settings)
                credentials.append(credential)
                blob_client = BlobServiceClient(
                    account_url=settings.blob.account_url or "", credential=credential
                )
            checks.append(BlobHealthCheck(blob_client))

        health_service = HealthService(checks, settings.health.dependency_timeout_seconds)
        return cls(
            settings=settings,
            sqlalchemy_engine=engine,
            redis=redis_client,
            health_service=health_service,
            neo4j=neo4j_driver,
            cosmos=cosmos_client,
            cosmos_database=cosmos_database,
            blob=blob_client,
            azure_credentials=credentials,
        )

    async def close(self) -> None:
        """Close all clients; tolerate clients with sync or async close methods."""
        resources: tuple[Any, ...] = (self.blob, self.cosmos, self.neo4j, self.redis)
        for resource in resources:
            if resource is None:
                continue
            close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        for credential in self.azure_credentials:
            await credential.close()
        await self.sqlalchemy_engine.dispose()


def _build_azure_credential(
    settings: AppSettings,
) -> DefaultAzureCredential | ClientSecretCredential:
    """Create the Azure credential configured for this deployment."""
    if settings.azure.authentication_mode == "client_secret":
        return ClientSecretCredential(
            tenant_id=settings.azure.tenant_id or "",
            client_id=settings.azure.client_id or "",
            client_secret=settings.azure.client_secret.get_secret_value()
            if settings.azure.client_secret is not None
            else "",
        )

    return DefaultAzureCredential(
        managed_identity_client_id=settings.azure.managed_identity_client_id,
        exclude_environment_credential=settings.azure.exclude_environment_credential,
        exclude_managed_identity_credential=settings.azure.exclude_managed_identity_credential,
    )
