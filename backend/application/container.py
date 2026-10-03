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
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from backend.application.health import HealthService
from backend.cache.redis import RedisCache
from backend.configuration.settings import AppSettings
from backend.db.engine import create_postgres_engine
from backend.db.health import PostgresHealthCheck
from backend.db.retry import RetryPolicy
from backend.db.session import DatabaseSessionManager, create_session_factory
from backend.events.workflow_streams import RedisWorkflowEventPublisher
from backend.infrastructure.azure.key_vault import AzureKeyVault
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
from backend.runtime import RuntimeFactory, RuntimeRegistry
from backend.runtime.middleware import RuntimeMiddlewarePipeline
from backend.telemetry import TelemetryHandle


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
    workflow_event_publisher: RedisWorkflowEventPublisher
    runtime_middleware_pipeline: RuntimeMiddlewarePipeline
    health_service: HealthService

    cosmos_connection: CosmosConnection | None = None
    neo4j_connection: Neo4jConnection | None = None
    neo4j: AsyncDriver | None = None
    neo4j_executor: Neo4jCypherExecutor | None = None
    neo4j_retry_policy: RetryPolicy | None = None
    runtime_registry: RuntimeRegistry = field(default_factory=RuntimeRegistry)
    runtime_factory: RuntimeFactory = field(init=False)

    cosmos: CosmosClient | None = None
    cosmos_database: DatabaseProxy | None = None
    cosmos_container_factory: CosmosContainerFactory | None = None
    cosmos_partition_strategy: CosmosPartitionStrategy | None = None
    blob: BlobServiceClient | None = None

    azure_credentials: list[
        DefaultAzureCredential | ClientSecretCredential
    ] = field(default_factory=list)

    @classmethod
    async def build(cls, settings: AppSettings) -> ApplicationContainer:
        """Construct application dependencies asynchronously."""
        return await cls.build_with_telemetry(settings, telemetry=None)

    @classmethod
    async def build_with_telemetry(
        cls,
        settings: AppSettings,
        telemetry: TelemetryHandle | None,
    ) -> ApplicationContainer:
        """Construct clients and resolve bootstrap secrets."""

        retry_policy = RetryPolicy()

        # Keep one shared Azure credential for all Azure services.
        credentials: list[
            DefaultAzureCredential | ClientSecretCredential
        ] = []

        azure_credential: (
            DefaultAzureCredential | ClientSecretCredential | None
        ) = None

        needs_azure_credential = (
            settings.key_vault.enabled
            or (
                settings.cosmos.enabled
                and settings.cosmos.connection_string is None
            )
            or (
                settings.blob.enabled
                and settings.blob.connection_string is None
            )
        )

        if needs_azure_credential:
            azure_credential = _build_azure_credential(settings)
            credentials.append(azure_credential)

        # Resolve the PostgreSQL password before constructing the engine.
        if settings.key_vault.enabled:
            if settings.key_vault.url is None:
                raise ValueError(
                    "Key Vault URL must be configured when Key Vault is enabled"
                )

            if azure_credential is None:
                raise RuntimeError(
                    "Azure credential was not initialized for Key Vault"
                )

            key_vault = AzureKeyVault(
                vault_url=settings.key_vault.url,
                credential=azure_credential,
            )

            try:
                password = await key_vault.get_secret(
                    settings.key_vault.postgres_password_secret
                )
            finally:
                await key_vault.close()

            settings.postgres.password = SecretStr(password)

        # PostgreSQL is created only after the Key Vault secret is resolved.
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
        workflow_event_publisher = RedisWorkflowEventPublisher(redis_streams)

        runtime_middleware_pipeline = RuntimeMiddlewarePipeline()
        runtime_registry: RuntimeRegistry = RuntimeRegistry()

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
            cosmos_credential: (
                DefaultAzureCredential | ClientSecretCredential | None
            ) = None

            if settings.cosmos.connection_string is None:
                if azure_credential is None:
                    raise RuntimeError(
                        "Azure credential was not initialized for Cosmos"
                    )

                cosmos_credential = azure_credential

            cosmos_client = create_cosmos_client(
                settings.cosmos,
                credential=cosmos_credential,
            )

            cosmos_connection = CosmosConnection(cosmos_client)

            cosmos_partition_strategy = CosmosPartitionStrategy(
                settings.cosmos.partition_key_path
            )

            cosmos_database = cosmos_connection.database(
                settings.cosmos.database_name
            )

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
                if azure_credential is None:
                    raise RuntimeError(
                        "Azure credential was not initialized for Blob"
                    )

                blob_client = BlobServiceClient(
                    account_url=settings.blob.account_url or "",
                    credential=azure_credential,
                )

            checks.append(BlobHealthCheck(blob_client))

        health_service = HealthService(
            checks,
            settings.health.dependency_timeout_seconds,
            telemetry=telemetry,
        )

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
            workflow_event_publisher=workflow_event_publisher,
            runtime_middleware_pipeline=runtime_middleware_pipeline,
            runtime_registry=runtime_registry,
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

            close = getattr(resource, "aclose", None) or getattr(
                resource,
                "close",
                None,
            )

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

    def __post_init__(self) -> None:
        """Derive the runtime factory from the injected registry."""
        self.runtime_factory = RuntimeFactory(self.runtime_registry)


def _build_azure_credential(
    settings: AppSettings,
) -> DefaultAzureCredential | ClientSecretCredential:
    """Create the Azure credential configured for this deployment."""

    if settings.azure.authentication_mode == "client_secret":
        return ClientSecretCredential(
            tenant_id=settings.azure.tenant_id or "",
            client_id=settings.azure.client_id or "",
            client_secret=(
                settings.azure.client_secret.get_secret_value()
                if settings.azure.client_secret is not None
                else ""
            ),
        )

    return DefaultAzureCredential(
        managed_identity_client_id=settings.azure.managed_identity_client_id,
        exclude_environment_credential=settings.azure.exclude_environment_credential,
        exclude_managed_identity_credential=settings.azure.exclude_managed_identity_credential,
    )