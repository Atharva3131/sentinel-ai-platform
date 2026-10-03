"""Dishka providers and container assembly for the application."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence

from azure.cosmos.aio import CosmosClient, DatabaseProxy
from azure.storage.blob.aio import BlobServiceClient
from dishka import Provider, Scope, from_context, make_async_container, provide
from dishka.async_container import AsyncContainer
from dishka.integrations.fastapi import FastapiProvider
from neo4j import AsyncDriver
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from backend.application.container import ApplicationContainer
from backend.application.health import HealthService
from backend.cache.redis import RedisCache
from backend.configuration.settings import (
    AppSettings,
    AzureCredentialSettings,
    FeatureFlagSettings,
    LLMSettings,
    LoggingSettings,
    OpenTelemetrySettings,
)
from backend.db.health import PostgresHealthCheck
from backend.db.repositories.incident import PostgreSQLIncidentRepository
from backend.db.retry import RetryPolicy
from backend.db.session import DatabaseSessionManager
from backend.evaluation.engine import EvaluationEngine
from backend.evaluation.registry import EvaluationRegistry
from backend.evaluation.strategies.investigation import register_investigation_strategies
from backend.events.workflow_streams import RedisWorkflowEventPublisher
from backend.infrastructure.cosmos import (
    CosmosConnection,
    CosmosContainerFactory,
    CosmosPartitionStrategy,
)
from backend.infrastructure.neo4j import Neo4jConnection, Neo4jCypherExecutor
from backend.infrastructure.redis import RedisConnection
from backend.interfaces.evidence import (
    LogsEvidenceProvider,
    MetricsEvidenceProvider,
    TracesEvidenceProvider,
)
from backend.interfaces.llm import LLMProvider
from backend.providers.evidence.factory import (
    build_logs_provider,
    build_metrics_provider,
    build_traces_provider,
)
from backend.providers.llm.factory import build_llm_provider
from backend.queues.redis import (
    RedisDeadLetterQueue,
    RedisLockManager,
    RedisPubSub,
    RedisRetryQueue,
    RedisStreamClient,
)
from backend.runtime import RuntimeFactory, RuntimeRegistry
from backend.runtime.middleware import RuntimeMiddlewarePipeline
from backend.services.evidence_orchestrator import EvidenceOrchestrator
from backend.services.incident_ingestion import IncidentIngestionService
from backend.services.investigation_evaluation import InvestigationEvaluator
from backend.telemetry import TelemetryHandle


class ApplicationProvider(Provider):
    """Application-scoped dependency providers."""

    scope = Scope.APP
    settings = from_context(AppSettings, scope=Scope.APP)
    telemetry = from_context(TelemetryHandle, scope=Scope.APP)

    @provide(scope=Scope.APP)
    async def application_container(
        self,
        settings: AppSettings,
        telemetry: TelemetryHandle,
    ) -> AsyncIterator[ApplicationContainer]:
        container = ApplicationContainer.build_with_telemetry(settings, telemetry)
        try:
            yield container
        finally:
            await container.close()

    @provide(scope=Scope.APP)
    def logging_settings(self, settings: AppSettings) -> LoggingSettings:
        return settings.logging

    @provide(scope=Scope.APP)
    def open_telemetry_settings(self, settings: AppSettings) -> OpenTelemetrySettings:
        return settings.opentelemetry

    @provide(scope=Scope.APP)
    def azure_credentials_settings(self, settings: AppSettings) -> AzureCredentialSettings:
        return settings.azure

    @provide(scope=Scope.APP)
    def evaluation_registry(self) -> EvaluationRegistry:
        """Build and return the EvaluationRegistry with all investigation strategies."""
        registry = EvaluationRegistry()
        register_investigation_strategies(registry)
        return registry

    @provide(scope=Scope.APP)
    def evaluation_engine(self, registry: EvaluationRegistry) -> EvaluationEngine:
        """Return an EvaluationEngine backed by the investigation strategy registry."""
        return EvaluationEngine(
            registry=registry,
            default_fail_fast=False,
            default_timeout_seconds=10.0,
        )

    @provide(scope=Scope.APP)
    def investigation_evaluator(self, engine: EvaluationEngine) -> InvestigationEvaluator:
        """Return the InvestigationEvaluator wired to the EvaluationEngine."""
        return InvestigationEvaluator(engine=engine)

    @provide(scope=Scope.APP)
    def metrics_evidence_provider(self, settings: AppSettings) -> MetricsEvidenceProvider:
        """Return the configured metrics evidence provider (fake or Prometheus)."""
        return build_metrics_provider(settings.evidence)

    @provide(scope=Scope.APP)
    def logs_evidence_provider(self, settings: AppSettings) -> LogsEvidenceProvider:
        """Return the configured logs evidence provider (fake or Elastic)."""
        return build_logs_provider(settings.evidence)

    @provide(scope=Scope.APP)
    def traces_evidence_provider(self, settings: AppSettings) -> TracesEvidenceProvider:
        """Return the configured traces evidence provider (fake or OTLP)."""
        return build_traces_provider(settings.evidence)

    @provide(scope=Scope.APP)
    def evidence_orchestrator(
        self,
        metrics: MetricsEvidenceProvider,
        logs: LogsEvidenceProvider,
        traces: TracesEvidenceProvider,
    ) -> EvidenceOrchestrator:
        """Return an EvidenceOrchestrator pre-wired with all three providers."""
        return EvidenceOrchestrator(
            providers=[metrics, logs, traces],
        )

    @provide(scope=Scope.APP)
    def llm_settings(self, settings: AppSettings) -> LLMSettings:
        return settings.llm

    @provide(scope=Scope.APP)
    def llm_provider(self, settings: AppSettings) -> LLMProvider:
        return build_llm_provider(settings.llm)

    @provide(scope=Scope.APP)
    def feature_flags(self, settings: AppSettings) -> FeatureFlagSettings:
        return settings.feature_flags

    @provide(scope=Scope.APP)
    def health_service(self, container: ApplicationContainer) -> HealthService:
        return container.health_service

    @provide(scope=Scope.APP)
    def sqlalchemy_engine(self, container: ApplicationContainer) -> AsyncEngine:
        return container.sqlalchemy_engine

    @provide(scope=Scope.APP)
    def session_factory(
        self,
        container: ApplicationContainer,
    ) -> Callable[[], AsyncSession]:
        return container.session_factory

    @provide(scope=Scope.APP)
    def session_manager(self, container: ApplicationContainer) -> DatabaseSessionManager:
        return container.session_manager

    @provide(scope=Scope.APP)
    def retry_policy(self, container: ApplicationContainer) -> RetryPolicy:
        return container.retry_policy

    @provide(scope=Scope.APP)
    def neo4j_retry_policy(self, container: ApplicationContainer) -> RetryPolicy:
        if container.neo4j_retry_policy is None:
            raise RuntimeError("Neo4j is disabled")
        return container.neo4j_retry_policy

    @provide(scope=Scope.APP)
    def redis(self, container: ApplicationContainer) -> Redis:
        return container.redis

    @provide(scope=Scope.APP)
    def redis_connection(self, container: ApplicationContainer) -> RedisConnection:
        return container.redis_connection

    @provide(scope=Scope.APP)
    def redis_cache(self, container: ApplicationContainer) -> RedisCache:
        return container.redis_cache

    @provide(scope=Scope.APP)
    def redis_streams(self, container: ApplicationContainer) -> RedisStreamClient:
        return container.redis_streams

    @provide(scope=Scope.APP)
    def redis_pubsub(self, container: ApplicationContainer) -> RedisPubSub:
        return container.redis_pubsub

    @provide(scope=Scope.APP)
    def redis_lock_manager(self, container: ApplicationContainer) -> RedisLockManager:
        return container.redis_lock_manager

    @provide(scope=Scope.APP)
    def redis_retry_queue(self, container: ApplicationContainer) -> RedisRetryQueue:
        return container.redis_retry_queue

    @provide(scope=Scope.APP)
    def redis_dead_letter_queue(self, container: ApplicationContainer) -> RedisDeadLetterQueue:
        return container.redis_dead_letter_queue

    @provide(scope=Scope.APP)
    def workflow_event_publisher(
        self,
        container: ApplicationContainer,
    ) -> RedisWorkflowEventPublisher:
        return container.workflow_event_publisher

    @provide(scope=Scope.APP)
    def runtime_middleware_pipeline(
        self,
        container: ApplicationContainer,
    ) -> RuntimeMiddlewarePipeline:
        return container.runtime_middleware_pipeline

    @provide(scope=Scope.APP)
    def runtime_registry(self, container: ApplicationContainer) -> RuntimeRegistry:
        return container.runtime_registry

    @provide(scope=Scope.APP)
    def runtime_factory(self, container: ApplicationContainer) -> RuntimeFactory:
        return container.runtime_factory

    @provide(scope=Scope.APP)
    def cosmos_connection(self, container: ApplicationContainer) -> CosmosConnection:
        if container.cosmos_connection is None:
            raise RuntimeError("Cosmos DB is disabled")
        return container.cosmos_connection

    @provide(scope=Scope.APP)
    def neo4j_connection(self, container: ApplicationContainer) -> Neo4jConnection:
        if container.neo4j_connection is None:
            raise RuntimeError("Neo4j is disabled")
        return container.neo4j_connection

    @provide(scope=Scope.APP)
    def neo4j_driver(self, container: ApplicationContainer) -> AsyncDriver:
        if container.neo4j is None:
            raise RuntimeError("Neo4j is disabled")
        return container.neo4j

    @provide(scope=Scope.APP)
    def neo4j_executor(self, container: ApplicationContainer) -> Neo4jCypherExecutor:
        if container.neo4j_executor is None:
            raise RuntimeError("Neo4j is disabled")
        return container.neo4j_executor

    @provide(scope=Scope.APP)
    def cosmos_client(self, container: ApplicationContainer) -> CosmosClient:
        if container.cosmos is None:
            raise RuntimeError("Cosmos DB is disabled")
        return container.cosmos

    @provide(scope=Scope.APP)
    def cosmos_database(self, container: ApplicationContainer) -> DatabaseProxy:
        if container.cosmos_database is None:
            raise RuntimeError("Cosmos DB is disabled")
        return container.cosmos_database

    @provide(scope=Scope.APP)
    def cosmos_container_factory(self, container: ApplicationContainer) -> CosmosContainerFactory:
        if container.cosmos_container_factory is None:
            raise RuntimeError("Cosmos DB is disabled")
        return container.cosmos_container_factory

    @provide(scope=Scope.APP)
    def cosmos_partition_strategy(self, container: ApplicationContainer) -> CosmosPartitionStrategy:
        if container.cosmos_partition_strategy is None:
            raise RuntimeError("Cosmos DB is disabled")
        return container.cosmos_partition_strategy

    @provide(scope=Scope.APP)
    def blob_service_client(self, container: ApplicationContainer) -> BlobServiceClient:
        if container.blob is None:
            raise RuntimeError("Blob Storage is disabled")
        return container.blob

    @provide(scope=Scope.REQUEST)
    def incident_repository(
        self,
        session: AsyncSession,
    ) -> PostgreSQLIncidentRepository:
        return PostgreSQLIncidentRepository(session)

    @provide(scope=Scope.REQUEST)
    def incident_ingestion_service(
        self,
        repository: PostgreSQLIncidentRepository,
    ) -> IncidentIngestionService:
        return IncidentIngestionService(repository=repository)

    @provide(scope=Scope.REQUEST)
    async def db_session(
        self,
        session_manager: DatabaseSessionManager,
    ) -> AsyncIterator[AsyncSession]:
        async with session_manager.session() as session:
            yield session

    @provide(scope=Scope.REQUEST)
    async def db_transaction(
        self,
        session_manager: DatabaseSessionManager,
    ) -> AsyncIterator[AsyncSession]:
        """Yield a session inside an explicit transaction.

        Use this when the route or service needs atomic multi-statement writes.
        The incident ingestion router uses db_session (auto-commit on flush);
        background batch operations should prefer db_transaction.
        """
        async with session_manager.transaction() as session:
            yield session

    @provide(scope=Scope.APP)
    def postgres_health_check(
        self,
        container: ApplicationContainer,
    ) -> PostgresHealthCheck:
        return PostgresHealthCheck(container.session_manager, container.retry_policy)


def build_root_container(
    settings: AppSettings,
    *,
    telemetry: TelemetryHandle,
    override_providers: Sequence[Provider] = (),
) -> AsyncContainer:
    """Create the Dishka root container with optional test overrides."""
    providers: tuple[Provider, ...] = (
        FastapiProvider(),
        ApplicationProvider(),
        *override_providers,
    )
    return make_async_container(
        *providers,
        context={
            AppSettings: settings,
            TelemetryHandle: telemetry,
        },
    )
