"""Dishka providers and container assembly for the application."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

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
    AzureMonitorMetricsSettings,
    DeploymentSettings,
    FeatureFlagSettings,
    GitHubSettings,
    LLMSettings,
    LoggingSettings,
    OpenTelemetrySettings,
)
from backend.core.hypothesis_engine import DeterministicHypothesisGenerator, HypothesisEngine
from backend.core.remediation_engine import RemediationEngine
from backend.core.validation_engine import VerificationEngine
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
from backend.infrastructure.neo4j_repository import Neo4jRepositoryBase
from backend.infrastructure.redis import RedisConnection
from backend.interfaces.deployment import DeploymentProvider
from backend.interfaces.evidence import (
    LogsEvidenceProvider,
    MetricsEvidenceProvider,
    TracesEvidenceProvider,
)
from backend.interfaces.llm import LLMProvider
from backend.policies.action_policy import RemediationPolicyEngine
from backend.providers.evidence.factory import (
    build_logs_provider,
    build_metrics_provider,
    build_traces_provider,
)
from backend.providers.github.client import GitHubClient
from backend.providers.github.deployment_factory import build_deployment_provider
from backend.providers.github.executor import GitHubActionExecutor
from backend.providers.github.planner import GitHubRemediationPlanner
from backend.providers.llm.factory import build_llm_provider
from backend.providers.metrics.azure_monitor_snapshot import AzureMonitorMetricsSnapshot
from backend.queues.redis import (
    RedisDeadLetterQueue,
    RedisLockManager,
    RedisPubSub,
    RedisRetryQueue,
    RedisStreamClient,
)
from backend.retrieval.graph import GraphRetriever
from backend.retrieval.pipeline import RetrievalPipeline
from backend.runtime import RuntimeFactory, RuntimeRegistry
from backend.runtime.middleware import RuntimeMiddlewarePipeline
from backend.services.closed_loop_orchestrator import ClosedLoopOrchestrator
from backend.services.deployment_pipeline import DeploymentPipeline
from backend.services.evidence_orchestrator import EvidenceOrchestrator
from backend.services.incident_ingestion import IncidentIngestionService
from backend.services.investigation_evaluation import InvestigationEvaluator
from backend.services.investigation_orchestrator import InvestigationOrchestrator
from backend.services.verification_coordinator import VerificationCoordinator
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
        container = await ApplicationContainer.build_with_telemetry(settings, telemetry)
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

    # ── Settings sub-objects ──────────────────────────────────────────────

    @provide(scope=Scope.APP)
    def deployment_settings(self, settings: AppSettings) -> DeploymentSettings:
        return settings.deployment

    @provide(scope=Scope.APP)
    def github_settings(self, settings: AppSettings) -> GitHubSettings:
        return settings.github

    # ── GitHub infrastructure ─────────────────────────────────────────────

    @provide(scope=Scope.APP)
    def github_client(self, settings: GitHubSettings) -> GitHubClient:
        """Return an async GitHub REST client configured from settings."""
        return GitHubClient.from_settings(settings)

    @provide(scope=Scope.APP)
    def github_action_executor(self, client: GitHubClient) -> GitHubActionExecutor:
        """Return the GitHub ActionExecutor backed by the shared client."""
        return GitHubActionExecutor(client)

    @provide(scope=Scope.APP)
    def deployment_provider(self, settings: AppSettings) -> DeploymentProvider:
        """Return the configured deployment provider (GitHub Actions or fake)."""
        return build_deployment_provider(settings)

    # ── Policy engine ─────────────────────────────────────────────────────

    @provide(scope=Scope.APP)
    def remediation_policy_engine(self, settings: AppSettings) -> RemediationPolicyEngine:
        """Return a RemediationPolicyEngine with optionally auto-approved HIGH-risk actions.
        
        When settings.remediation.auto_approve_high_risk is True, HIGH-risk actions
        are auto-approved without human review (demo/autonomous mode).
        """
        from backend.models.remediation import ActionRiskLevel
        from backend.policies.action_policy import ActionPolicy
        
        # Build auto-approve levels based on configuration
        auto_approve_levels = {ActionRiskLevel.LOW, ActionRiskLevel.MEDIUM}
        if settings.remediation.auto_approve_high_risk:
            auto_approve_levels.add(ActionRiskLevel.HIGH)
        
        # Create policy with appropriate risk approval levels
        policy = ActionPolicy(
            auto_approve_risk_levels=frozenset(auto_approve_levels)
        )
        
        return RemediationPolicyEngine(policy=policy)

    # ── Hypothesis engine ─────────────────────────────────────────────────

    @provide(scope=Scope.APP)
    def hypothesis_engine(self) -> HypothesisEngine:
        """Return a HypothesisEngine backed by the deterministic heuristic generator."""
        return HypothesisEngine(generator=DeterministicHypothesisGenerator())

    # ── InvestigationOrchestrator ─────────────────────────────────────────

    @provide(scope=Scope.APP)
    def graph_repository(
        self,
        container: ApplicationContainer,
    ) -> Neo4jRepositoryBase | None:
        """Return a Neo4j repository for graph traversal, or None when disabled."""
        if container.neo4j_executor is None:
            return None
        return Neo4jRepositoryBase(executor=container.neo4j_executor)

    @provide(scope=Scope.APP)
    def graph_retrieval_pipeline(
        self,
        settings: AppSettings,
        graph_repo: Neo4jRepositoryBase | None,
        redis_cache: RedisCache,
    ) -> RetrievalPipeline | None:
        """Return a graph RetrievalPipeline when GraphRAG is enabled and Neo4j is available.

        Returns None when:
        - ``feature_flags.graph_rag`` is False, or
        - Neo4j is not enabled/connected (graph_repo is None).
        This makes GraphRAG strictly optional; the investigation pipeline
        falls back to evidence-only mode automatically.
        """
        if not settings.feature_flags.graph_rag:
            return None
        if graph_repo is None:
            return None
        retriever = GraphRetriever(repository=graph_repo, cache=redis_cache)
        return RetrievalPipeline(provider=retriever, cache=redis_cache)

    @provide(scope=Scope.APP)
    def investigation_orchestrator(
        self,
        evidence_orchestrator: EvidenceOrchestrator,
        hypothesis_engine: HypothesisEngine,
        event_publisher: RedisWorkflowEventPublisher,
        graph_pipeline: RetrievalPipeline | None,
    ) -> InvestigationOrchestrator:
        """Return the investigation pipeline wired to evidence + hypothesis engines.

        When GraphRAG is enabled and Neo4j is available, ``graph_pipeline`` is
        injected so the orchestrator enriches investigation context with graph
        knowledge before each evidence loop.
        """
        return InvestigationOrchestrator(
            evidence_orchestrator=evidence_orchestrator,
            hypothesis_engine=hypothesis_engine,
            event_emitter=event_publisher,
            retrieval_pipeline=graph_pipeline,
        )

    # ── RemediationEngine ─────────────────────────────────────────────────

    @provide(scope=Scope.APP)
    def remediation_engine(
        self,
        policy_engine: RemediationPolicyEngine,
        executor: GitHubActionExecutor,
    ) -> RemediationEngine:
        """Return a policy-gated RemediationEngine using the GitHub executor."""
        return RemediationEngine(
            policy_gateway=policy_engine,
            executor=executor,
        )

    # ── GitHubRemediationPlanner ──────────────────────────────────────────

    @provide(scope=Scope.APP)
    def github_remediation_planner(self, settings: GitHubSettings) -> GitHubRemediationPlanner:
        """Return a planner pre-configured with the default GitHub owner/repo."""
        return GitHubRemediationPlanner(
            owner=settings.default_owner,
            repo=settings.default_repository,
        )

    # ── DeploymentPipeline ────────────────────────────────────────────────

    @provide(scope=Scope.APP)
    def deployment_pipeline(
        self,
        provider: DeploymentProvider,
        policy_engine: RemediationPolicyEngine,
        event_publisher: RedisWorkflowEventPublisher,
    ) -> DeploymentPipeline:
        """Return the deployment pipeline gated by the remediation policy engine."""
        return DeploymentPipeline(
            provider=provider,
            policy_engine=policy_engine,
            event_emitter=event_publisher,
        )

    # ── Azure Monitor Metrics settings ────────────────────────────────────

    @provide(scope=Scope.APP)
    def azure_monitor_metrics_settings(
        self, settings: AppSettings
    ) -> AzureMonitorMetricsSettings:
        return settings.azure_monitor_metrics

    # ── MetricsSnapshotPort (AzureMonitorMetricsSnapshot) ─────────────────

    @provide(scope=Scope.APP)
    def metrics_snapshot(
        self,
        az_settings: AzureMonitorMetricsSettings,
        app_settings: AppSettings,
    ) -> AzureMonitorMetricsSnapshot:
        """Return the production MetricsSnapshotPort backed by Application Insights.

        When ``azure_monitor_metrics.enabled`` is False (the default), the adapter
        is constructed with an empty ``app_id``-guard bypassed by an empty
        ``app_id`` — callers receive 0.0 for all metrics, which is the safe
        default (no degradation detected).  Enable and configure
        ``SENTINEL_AZURE_MONITOR_METRICS__*`` environment variables to activate
        live querying.
        """
        if not az_settings.enabled or not az_settings.app_id:
            # Disabled / unconfigured — return a no-op snapshot that always
            # returns 0.0.  We do NOT raise here so that environments without
            # Application Insights wired (dev, CI) still construct correctly.
            return _NoOpMetricsSnapshot()  # type: ignore[return-value]

        # Build the Azure credential using the same factory as ApplicationContainer
        from azure.identity.aio import ClientSecretCredential, DefaultAzureCredential

        azure_cfg = app_settings.azure
        if azure_cfg.authentication_mode == "client_secret":
            credential: DefaultAzureCredential | ClientSecretCredential = (
                ClientSecretCredential(
                    tenant_id=azure_cfg.tenant_id or "",
                    client_id=azure_cfg.client_id or "",
                    client_secret=(
                        azure_cfg.client_secret.get_secret_value()
                        if azure_cfg.client_secret is not None else ""
                    ),
                )
            )
        else:
            credential = DefaultAzureCredential(
                managed_identity_client_id=azure_cfg.managed_identity_client_id,
                exclude_environment_credential=azure_cfg.exclude_environment_credential,
                exclude_managed_identity_credential=(
                    azure_cfg.exclude_managed_identity_credential
                ),
            )

        return AzureMonitorMetricsSnapshot.from_settings(az_settings, credential)

    # ── VerificationEngine ────────────────────────────────────────────────

    @provide(scope=Scope.APP)
    def verification_engine(
        self,
        snapshot: AzureMonitorMetricsSnapshot,
    ) -> VerificationEngine:
        """Return a VerificationEngine backed by the Azure Monitor snapshot adapter."""
        return VerificationEngine(metrics_snapshot=snapshot)

    # ── VerificationCoordinator ───────────────────────────────────────────

    @provide(scope=Scope.APP)
    def verification_coordinator(
        self,
        verification_engine: VerificationEngine,
        evidence_orchestrator: EvidenceOrchestrator,
        event_publisher: RedisWorkflowEventPublisher,
    ) -> VerificationCoordinator:
        """Return the post-deployment verification coordinator."""
        return VerificationCoordinator(
            verification_engine=verification_engine,
            evidence_orchestrator=evidence_orchestrator,
            event_emitter=event_publisher,
        )

    # ── Azure Remediation Service ─────────────────────────────────────────

    @provide(scope=Scope.APP)
    def azure_remediation_service(
        self,
        settings: AppSettings,
    ) -> Any:
        """Return the Azure Container Apps remediation service for Demo 1 incidents.

        Returns None if disabled; this allows ClosedLoopOrchestrator to skip Azure
        remediation checks gracefully when the feature is not configured or active.

        When enabled, constructs AzureRemediationService with the configured settings.
        When disabled, returns None to safely short-circuit all Azure logic.
        """
        if not settings.azure_remediation.enabled:
            return None

        from backend.services.azure_remediation import AzureRemediationService
        return AzureRemediationService(settings.azure_remediation)

    # ── ClosedLoopOrchestrator ────────────────────────────────────────────

    @provide(scope=Scope.REQUEST)
    def closed_loop_orchestrator(
        self,
        investigation: InvestigationOrchestrator,
        remediation_engine: RemediationEngine,
        deployment_pipeline: DeploymentPipeline,
        verification: VerificationCoordinator,
        repository: PostgreSQLIncidentRepository,
        planner: GitHubRemediationPlanner,
        event_publisher: RedisWorkflowEventPublisher,
        settings: AppSettings,
        azure_remediation_service: Any,
    ) -> ClosedLoopOrchestrator:
        """Return the fully wired autonomous closed-loop orchestrator.

        REQUEST scope so it receives the live per-request PostgreSQLIncidentRepository
        (backed by the current AsyncSession) without any dataclasses.replace or
        field mutation.  All other dependencies are APP-scoped — Dishka resolves
        wider scopes into narrower ones automatically.
        """
        return ClosedLoopOrchestrator(
            investigation=investigation,
            remediation_engine=remediation_engine,
            deployment_pipeline=deployment_pipeline,
            verification=verification,
            repository=repository,
            remediation_planner=planner.plan,
            event_emitter=event_publisher,
            azure_remediation_service=azure_remediation_service,
            deployment_owner=settings.github.default_owner,
            deployment_repo=settings.github.default_repository,
            deployment_workflow=settings.deployment.workflow,
            deployment_environment=settings.deployment.environment,
        )

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


# ---------------------------------------------------------------------------
# Sentinel helper — not a fake, not a test double; this is a structural guard
# that makes the disabled/unconfigured Azure Monitor path safe at the type level.
# ---------------------------------------------------------------------------


class _NoOpMetricsSnapshot:
    """Returned when Azure Monitor metrics querying is disabled or unconfigured.

    Always returns 0.0 for every requested metric, which the VerificationEngine
    treats as "no degradation detected / unknown state".  This is the correct
    safe default for environments that have not wired Application Insights
    (development, CI, staging without observability).

    This is NOT a fake — it carries no test-specific behaviour and lives in
    production code.  It satisfies MetricsSnapshotPort structurally.
    """

    async def snapshot(
        self,
        service: str,
        metric_names: tuple[str, ...],
        *,
        window_seconds: float = 60.0,
    ) -> dict[str, float]:
        return {name: 0.0 for name in metric_names}
