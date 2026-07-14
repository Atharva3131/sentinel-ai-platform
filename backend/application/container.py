"""Application dependency container and resource lifecycle."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from azure.cosmos.aio import CosmosClient, DatabaseProxy
from azure.identity.aio import ClientSecretCredential, DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient
from neo4j import AsyncDriver
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from backend.application.health import HealthService
from backend.cache.redis import RedisCache
from backend.configuration.settings import AppSettings
from backend.db.engine import create_postgres_engine
from backend.db.health import PostgresHealthCheck
from backend.db.retry import RetryPolicy
from backend.db.session import DatabaseSessionManager, create_session_factory
from backend.infrastructure.cosmos import (
    CosmosConnection,
    CosmosContainerFactory,
    CosmosPartitionStrategy,
    create_cosmos_client,
)
from backend.infrastructure.health_checks import (
    BlobHealthCheck,
    CosmosHealthCheck,
    Neo4jHealthCheck,
    RedisHealthCheck,
)
from backend.infrastructure.neo4j import (
    Neo4jConnection,
    Neo4jCypherExecutor,
    create_neo4j_driver,
    create_neo4j_retry_policy,
)
from backend.infrastructure.redis import RedisConnection, create_redis_client
from backend.interfaces.health import HealthCheck
from backend.queues.redis import (
    RedisDeadLetterQueue,
    RedisLockManager,
    RedisPubSub,
    RedisRetryQueue,
    RedisStreamClient,
)


@dataclass(slots=True)
class ApplicationContainer:
    """Own application dependencies and close them in a deterministic order."""

    settings: AppSettings
    sqlalchemy_engine: AsyncEngine
    session_factory: Callable[[], AsyncSession]
    session_manager: DatabaseSessionManager
    retry_policy: RetryPolicy
    redis_connection: RedisConnection
    redis: Redis
    redis_cache: RedisCache
    redis_streams: RedisStreamClient
    redis_pubsub: RedisPubSub
    redis_lock_manager: RedisLockManager
    redis_retry_queue: RedisRetryQueue
    redis_dead_letter_queue: RedisDeadLetterQueue
    health_service: HealthService
    cosmos_connection: CosmosConnection | None = None
    neo4j_connection: Neo4jConnection | None = None
    neo4j: AsyncDriver | None = None
    neo4j_executor: Neo4jCypherExecutor | None = None
    neo4j_retry_policy: RetryPolicy | None = None
    cosmos: CosmosClient | None = None
    cosmos_database: DatabaseProxy | None = None
    cosmos_container_factory: CosmosContainerFactory | None = None
    cosmos_partition_strategy: CosmosPartitionStrategy | None = None
    blob: BlobServiceClient | None = None
    azure_credentials: list[DefaultAzureCredential | ClientSecretCredential] = field(
        default_factory=list
    )

    @classmethod
    def build(cls, settings: AppSettings) -> ApplicationContainer:
        """Construct lazy clients without performing network I/O."""
        retry_policy = RetryPolicy()
        engine = create_postgres_engine(settings.postgres)
        session_factory = create_session_factory(engine)
        session_manager = DatabaseSessionManager(
            engine=engine,
            session_factory=session_factory,
        )
        redis_client = create_redis_client(settings.redis)
        redis_connection = RedisConnection(redis_client)
        redis_cache = RedisCache(redis_connection)
        redis_streams = RedisStreamClient(redis_connection)
        redis_pubsub = RedisPubSub(redis_connection)
        redis_lock_manager = RedisLockManager(redis_connection)
        redis_retry_queue = RedisRetryQueue(redis_streams)
        redis_dead_letter_queue = RedisDeadLetterQueue(redis_streams)

        checks: list[HealthCheck] = [
            PostgresHealthCheck(session_manager, retry_policy),
            RedisHealthCheck(redis_connection),
        ]
        neo4j_driver: AsyncDriver | None = None
        neo4j_connection: Neo4jConnection | None = None
        neo4j_executor: Neo4jCypherExecutor | None = None
        neo4j_retry_policy: RetryPolicy | None = None
        cosmos_client: CosmosClient | None = None
        cosmos_connection: CosmosConnection | None = None
        cosmos_database: DatabaseProxy | None = None
        cosmos_container_factory: CosmosContainerFactory | None = None
        cosmos_partition_strategy: CosmosPartitionStrategy | None = None
        blob_client: BlobServiceClient | None = None
        credentials: list[DefaultAzureCredential | ClientSecretCredential] = []

        if settings.neo4j.enabled:
            neo4j_driver = create_neo4j_driver(settings.neo4j)
            neo4j_connection = Neo4jConnection(
                driver=neo4j_driver,
                database=settings.neo4j.database,
            )
            neo4j_retry_policy = create_neo4j_retry_policy()
            neo4j_executor = Neo4jCypherExecutor(
                connection=neo4j_connection,
                retry_policy=neo4j_retry_policy,
            )
            checks.append(Neo4jHealthCheck(neo4j_connection))

        if settings.cosmos.enabled:
            cosmos_credential: DefaultAzureCredential | ClientSecretCredential | None = None
            if settings.cosmos.connection_string is None:
                cosmos_credential = _build_azure_credential(settings)
                credentials.append(cosmos_credential)
            cosmos_client = create_cosmos_client(
                settings.cosmos,
                credential=cosmos_credential,
            )
            cosmos_connection = CosmosConnection(cosmos_client)
            cosmos_partition_strategy = CosmosPartitionStrategy(settings.cosmos.partition_key_path)
            cosmos_database = cosmos_connection.database(settings.cosmos.database_name)
            cosmos_container_factory = CosmosContainerFactory(
                connection=cosmos_connection,
                database_name=settings.cosmos.database_name,
                container_name=settings.cosmos.container_name,
                partition_strategy=cosmos_partition_strategy,
            )
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
            session_factory=session_factory,
            session_manager=session_manager,
            retry_policy=retry_policy,
            redis_connection=redis_connection,
            redis=redis_client,
            redis_cache=redis_cache,
            redis_streams=redis_streams,
            redis_pubsub=redis_pubsub,
            redis_lock_manager=redis_lock_manager,
            redis_retry_queue=redis_retry_queue,
            redis_dead_letter_queue=redis_dead_letter_queue,
            health_service=health_service,
            cosmos_connection=cosmos_connection,
            neo4j_connection=neo4j_connection,
            neo4j=neo4j_driver,
            neo4j_executor=neo4j_executor,
            neo4j_retry_policy=neo4j_retry_policy,
            cosmos=cosmos_client,
            cosmos_database=cosmos_database,
            cosmos_container_factory=cosmos_container_factory,
            cosmos_partition_strategy=cosmos_partition_strategy,
            blob=blob_client,
            azure_credentials=credentials,
        )

    async def close(self) -> None:
        """Close all clients; tolerate clients with sync or async close methods."""
        resources: tuple[Any, ...] = (self.blob,)
        for resource in resources:
            if resource is None:
                continue
            close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        if self.cosmos_connection is not None:
            await self.cosmos_connection.close()
        if self.neo4j_connection is not None:
            await self.neo4j_connection.close()
        await self.redis_connection.close()
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
