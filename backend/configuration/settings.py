"""Typed, environment-aware application configuration."""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import quote

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from pydantic_settings.sources.providers.nested_secrets import NestedSecretsSettingsSource

DEFAULT_ENV_FILE = Path(".env")


class DeploymentEnvironment(StrEnum):
    """Supported deployment environments."""

    DEVELOPMENT = "development"
    TESTING = "testing"
    PRODUCTION = "production"


class ServerSettings(BaseModel):
    """HTTP server settings."""

    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    workers: int = Field(default=1, ge=1)


class LoggingSettings(BaseModel):
    """Structured logging settings."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    json_output: bool = True


class OpenTelemetrySettings(BaseModel):
    """OpenTelemetry exporter and sampling settings."""

    enabled: bool = True
    otlp_http_endpoint: str | None = None
    otlp_headers: SecretStr | None = None
    trace_sample_ratio: float = Field(default=0.1, ge=0.0, le=1.0)
    export_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    exclude_health_endpoints: bool = True
    azure_monitor_connection_string: SecretStr | None = None


class AzureCredentialSettings(BaseModel):
    """Azure authentication settings for SDK clients."""

    authentication_mode: Literal["default", "client_secret"] = "default"
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: SecretStr | None = None
    managed_identity_client_id: str | None = None
    exclude_environment_credential: bool = False
    exclude_managed_identity_credential: bool = False

    @model_validator(mode="after")
    def validate_authentication_mode(self) -> Self:
        """Require a complete service principal when client-secret auth is selected."""
        if self.authentication_mode == "client_secret":
            if not self.tenant_id or not self.client_id or not self.client_secret:
                raise ValueError(
                    "azure authentication_mode=client_secret requires "
                    "tenant_id, client_id, and client_secret"
                )
        return self


class KeyVaultSettings(BaseModel):
    """Azure Key Vault configuration."""

    enabled: bool = False
    url: str | None = None
    postgres_password_secret: str = "postgres-admin-password"
    llm_primary_api_key_secret: str = "llm-primary-api-key"

    @model_validator(mode="after")
    def validate_enabled(self) -> Self:
        if self.enabled and not self.url:
            raise ValueError(
                "key_vault.url must be configured when Key Vault is enabled"
            )
        return self


class PostgresSettings(BaseModel):
    """PostgreSQL connectivity and pool settings."""

    driver: str = "postgresql+asyncpg"
    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = "sentinel"
    username: str = "sentinel"
    password: SecretStr = SecretStr("sentinel")
    url: SecretStr | None = None
    pool_size: int = Field(default=10, ge=1)
    max_overflow: int = Field(default=20, ge=0)
    pool_timeout_seconds: float = Field(default=10.0, gt=0)
    pool_recycle_seconds: int = Field(default=1800, ge=60)

    @property
    def sqlalchemy_url(self) -> str:
        """Return the connection URL consumed by SQLAlchemy."""
        if self.url is not None:
            return self.url.get_secret_value()

        username = quote(self.username, safe="")
        password = quote(self.password.get_secret_value(), safe="")
        database = quote(self.database, safe="")
        return f"{self.driver}://{username}:{password}@{self.host}:{self.port}/{database}"


class RedisSettings(BaseModel):
    """Redis connectivity and pool settings."""

    host: str = "localhost"
    port: int = Field(default=6379, ge=1, le=65535)
    database: int = Field(default=0, ge=0)
    username: str | None = None
    password: SecretStr | None = None
    ssl: bool = False
    url: SecretStr | None = None
    socket_timeout_seconds: float = Field(default=5.0, gt=0)
    socket_connect_timeout_seconds: float = Field(default=3.0, gt=0)
    max_connections: int = Field(default=100, ge=1)

    @property
    def redis_url(self) -> str:
        """Return the connection URL consumed by redis-py."""
        if self.url is not None:
            return self.url.get_secret_value()

        scheme = "rediss" if self.ssl else "redis"
        username = quote(self.username, safe="") if self.username else ""
        password = self.password.get_secret_value() if self.password else None
        auth = ""
        if username and password is not None:
            auth = f"{username}:{quote(password, safe='')}@"
        elif username:
            auth = f"{username}@"
        elif password is not None:
            auth = f":{quote(password, safe='')}@"
        return f"{scheme}://{auth}{self.host}:{self.port}/{self.database}"


class Neo4jSettings(BaseModel):
    """Neo4j connectivity settings."""

    enabled: bool = False
    scheme: Literal["neo4j", "neo4j+s", "bolt", "bolt+s"] = "neo4j"
    host: str = "localhost"
    port: int = Field(default=7687, ge=1, le=65535)
    database: str = "neo4j"
    username: str = "neo4j"
    password: SecretStr = SecretStr("neo4j")
    uri: str | None = None
    connection_timeout_seconds: float = Field(default=5.0, gt=0)
    max_connection_pool_size: int = Field(default=50, ge=1)

    @property
    def driver_uri(self) -> str:
        """Return the connection URI consumed by the Neo4j driver."""
        return self.uri or f"{self.scheme}://{self.host}:{self.port}"


class CosmosSettings(BaseModel):
    """Azure Cosmos DB settings."""

    enabled: bool = False
    endpoint: str | None = None
    database_name: str = "sentinel"
    container_name: str = "sentinel"
    partition_key_path: str = "/partitionKey"
    connection_string: SecretStr | None = None
    preferred_locations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        """Require either a connection string or endpoint when Cosmos is enabled."""
        if self.enabled and not self.connection_string and not self.endpoint:
            raise ValueError(
                "cosmos requires connection_string or endpoint when enabled"
            )
        return self


class BlobStorageSettings(BaseModel):
    """Azure Blob Storage settings."""

    enabled: bool = False
    account_url: str | None = None
    connection_string: SecretStr | None = None
    container_name: str = "sentinel"

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        """Require either a connection string or account URL when enabled."""
        if self.enabled and not self.connection_string and not self.account_url:
            raise ValueError(
                "blob requires connection_string or account_url when enabled"
            )
        return self


class HealthSettings(BaseModel):
    """Health-probe execution settings."""

    dependency_timeout_seconds: float = Field(default=3.0, gt=0, le=30.0)


class FeatureFlagSettings(BaseModel):
    """Feature toggles for progressive delivery and safe rollouts."""

    runtime_abstraction: bool = True
    graph_rag: bool = False
    evaluation_pipeline: bool = False
    recovery_engine: bool = False
    notification_delivery: bool = False
    audit_trail: bool = True


class AzureMonitorMetricsSettings(BaseModel):
    """Azure Monitor / Application Insights metrics query configuration.

    Used by ``AzureMonitorMetricsSnapshot`` to query per-service metric values
    for closed-loop verification.

    Authentication priority:
      1. ``api_key`` — API key sent as ``x-api-key`` (no AAD needed).
      2. AAD credential from ``AzureCredentialSettings`` when ``api_key`` not set.

    Environment variable examples::

        SENTINEL_AZURE_MONITOR_METRICS__ENABLED=true
        SENTINEL_AZURE_MONITOR_METRICS__APP_ID=<app-id>
        SENTINEL_AZURE_MONITOR_METRICS__API_KEY=<key>
    """

    enabled: bool = False
    app_id: str = ""
    api_key: SecretStr | None = None
    base_url: str = "https://api.applicationinsights.io"
    timeout_seconds: float = Field(default=30.0, gt=0.0)
    max_retries: int = Field(default=3, ge=0)
    retry_min_wait_seconds: float = Field(default=0.5, gt=0.0)
    retry_max_wait_seconds: float = Field(default=10.0, gt=0.0)


class DeploymentProviderName(StrEnum):
    """Supported deployment provider names."""

    GITHUB_ACTIONS = "github_actions"
    FAKE = "fake"


class DeploymentSettings(BaseModel):
    """Deployment provider configuration.

    Disabled by default. Set ``enabled=true`` and configure the provider
    to activate automated deployment triggering.

    Environment variable examples::

        SENTINEL_DEPLOYMENT__ENABLED=true
        SENTINEL_DEPLOYMENT__PROVIDER=github_actions
        SENTINEL_DEPLOYMENT__WORKFLOW=deploy.yml
        SENTINEL_DEPLOYMENT__ENVIRONMENT=staging
        SENTINEL_DEPLOYMENT__TIMEOUT_SECONDS=600
        SENTINEL_DEPLOYMENT__POLL_INTERVAL_SECONDS=10
    """

    enabled: bool = False
    provider: DeploymentProviderName = DeploymentProviderName.FAKE
    workflow: str = "deploy.yml"
    environment: str = "staging"
    timeout_seconds: float = Field(default=600.0, gt=0.0)
    poll_interval_seconds: float = Field(default=15.0, gt=0.0)
    max_retries: int = Field(default=3, ge=0)
    retry_min_wait_seconds: float = Field(default=0.5, gt=0.0)
    retry_max_wait_seconds: float = Field(default=10.0, gt=0.0)


class RemediationSettings(BaseModel):
    """Remediation policy and execution configuration.

    Controls whether HIGH-risk actions are auto-approved without human review.

    Environment variable examples::

        SENTINEL_REMEDIATION__AUTO_APPROVE_HIGH_RISK=false
    """

    auto_approve_high_risk: bool = Field(
        default=False,
        description=(
            "Auto-approve HIGH-risk remediation actions without human approval"
        ),
    )


class GitHubSettings(BaseModel):
    """GitHub API configuration for the remediation integration.

    Disabled by default — set ``enabled=true`` to activate.
    The ``token`` must arrive through environment variables or secret files.

    Environment variable examples::

        SENTINEL_GITHUB__ENABLED=true
        SENTINEL_GITHUB__TOKEN=ghp_...
        SENTINEL_GITHUB__DEFAULT_OWNER=my-org
        SENTINEL_GITHUB__DEFAULT_REPOSITORY=my-repo
    """

    enabled: bool = False
    base_url: str = "https://api.github.com"
    token: SecretStr | None = None
    default_owner: str = ""
    default_repository: str = ""
    timeout_seconds: float = Field(default=30.0, gt=0.0)
    max_retries: int = Field(default=3, ge=0)
    retry_min_wait_seconds: float = Field(default=0.5, gt=0.0)
    retry_max_wait_seconds: float = Field(default=10.0, gt=0.0)
    per_page: int = Field(default=30, ge=1, le=100)


class EvidenceProviderName(StrEnum):
    """Supported evidence provider names."""

    PROMETHEUS = "prometheus"
    ELASTIC = "elastic"
    OTLP = "otlp"
    AZURE_MONITOR = "azure_monitor"
    FAKE = "fake"


class EvidenceProviderSettings(BaseModel):
    """Shared settings for a single evidence source provider.

    Credentials arrive only through environment variables / secret files.
    """

    name: EvidenceProviderName = EvidenceProviderName.FAKE
    enabled: bool = True
    base_url: str = ""
    api_key: SecretStr | None = None
    username: str | None = None
    password: SecretStr | None = None
    timeout_seconds: float = Field(default=30.0, gt=0.0)
    max_retries: int = Field(default=3, ge=0)
    retry_min_wait_seconds: float = Field(default=0.5, gt=0.0)
    retry_max_wait_seconds: float = Field(default=5.0, gt=0.0)
    default_window_seconds: float = Field(default=300.0, gt=0.0)
    max_items: int = Field(default=20, ge=1)


class EvidenceSettings(BaseModel):
    """Evidence collection provider configuration.

    Each source kind (metrics, logs, traces) has its own provider settings.

    Environment variable examples::

        SENTINEL_EVIDENCE__METRICS__NAME=prometheus
        SENTINEL_EVIDENCE__METRICS__BASE_URL=http://prometheus:9090
        SENTINEL_EVIDENCE__LOGS__NAME=elastic
        SENTINEL_EVIDENCE__LOGS__BASE_URL=http://elasticsearch:9200
        SENTINEL_EVIDENCE__TRACES__NAME=otlp
        SENTINEL_EVIDENCE__TRACES__BASE_URL=http://jaeger:16686
    """

    metrics: EvidenceProviderSettings = Field(
        default_factory=EvidenceProviderSettings
    )
    logs: EvidenceProviderSettings = Field(
        default_factory=EvidenceProviderSettings
    )
    traces: EvidenceProviderSettings = Field(
        default_factory=EvidenceProviderSettings
    )


class LLMProviderName(StrEnum):
    """Supported LLM provider names."""

    SARVAM = "sarvam"
    MISTRAL = "mistral"
    FAKE = "fake"


class LLMProviderSettings(BaseModel):
    """Configuration for one LLM provider (primary or fallback)."""

    name: LLMProviderName = LLMProviderName.FAKE
    api_key: SecretStr | None = None
    model: str = "sarvam-105b"
    base_url: str = "https://api.sarvam.ai/v1"
    timeout_seconds: float = Field(default=60.0, gt=0.0)
    max_retries: int = Field(default=3, ge=0)
    retry_min_wait_seconds: float = Field(default=1.0, gt=0.0)
    retry_max_wait_seconds: float = Field(default=10.0, gt=0.0)
    temperature: float | None = None
    max_output_tokens: int | None = None


class LLMSettings(BaseModel):
    """LLM provider configuration — primary and optional fallback."""

    primary: LLMProviderSettings = Field(
        default_factory=LLMProviderSettings
    )
    fallback: LLMProviderSettings | None = None
    enable_fallback: bool = True


class AzureRemediationSettings(BaseModel):
    """Azure Container Apps remediation configuration for Demo 1 incidents.

    Handles automatic restart of Container Apps to recover from connection-leak
    incidents. Requires explicit Demo 1 correlation ID pattern matching.

    Environment variable examples::

        SENTINEL_AZURE_REMEDIATION__ENABLED=true
        SENTINEL_AZURE_REMEDIATION__RESOURCE_GROUP=sentinel-ai-rg
        SENTINEL_AZURE_REMEDIATION__CONTAINER_APP_NAME=demo1-order-api-standard
        SENTINEL_AZURE_REMEDIATION__HEALTH_CHECK_URL=https://demo1.../health
        SENTINEL_AZURE_REMEDIATION__CORRELATION_ID_PATTERN=demo1-.*-connection_leak-.*
        SENTINEL_AZURE_REMEDIATION__MANAGED_IDENTITY_CLIENT_ID=<client-id>
    """

    enabled: bool = False
    resource_group: str = ""
    container_app_name: str = ""
    health_check_url: str = ""
    correlation_id_pattern: str = "demo1-.*-connection_leak-.*"
    max_restart_attempts: int = Field(default=3, ge=1, le=10)
    health_check_timeout_seconds: float = Field(default=30.0, gt=0.0)
    health_check_poll_interval_seconds: float = Field(default=5.0, gt=0.0)
    health_check_max_duration_seconds: float = Field(default=120.0, gt=0.0)
    managed_identity_client_id: str | None = Field(
        default=None,
        description="Azure Managed Identity client ID. Required when enabled=true.",
    )


class AppSettings(BaseSettings):
    """Root application settings from environment variables and secret files."""

    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="SENTINEL_",
        env_nested_delimiter="__",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "sentinel-ai-platform"
    app_version: str = "0.1.0"
    environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT
    debug: bool = False
    api_prefix: str = "/api/v1"
    server: ServerSettings = Field(default_factory=ServerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    opentelemetry: OpenTelemetrySettings = Field(
        default_factory=OpenTelemetrySettings
    )
    azure: AzureCredentialSettings = Field(default_factory=AzureCredentialSettings)
    key_vault: KeyVaultSettings = Field(default_factory=KeyVaultSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    cosmos: CosmosSettings = Field(default_factory=CosmosSettings)
    blob: BlobStorageSettings = Field(default_factory=BlobStorageSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    feature_flags: FeatureFlagSettings = Field(default_factory=FeatureFlagSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    evidence: EvidenceSettings = Field(default_factory=EvidenceSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    deployment: DeploymentSettings = Field(default_factory=DeploymentSettings)
    remediation: RemediationSettings = Field(default_factory=RemediationSettings)
    azure_monitor_metrics: AzureMonitorMetricsSettings = Field(
        default_factory=AzureMonitorMetricsSettings
    )
    azure_remediation: AzureRemediationSettings = Field(
        default_factory=AzureRemediationSettings
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Load in order: kwargs, env, dotenv, then secret files."""
        nested_secret_settings = NestedSecretsSettingsSource(
            file_secret_settings,
            secrets_nested_delimiter="__",
            secrets_dir_missing="ok",
        )
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            nested_secret_settings,
        )

    @model_validator(mode="after")
    def validate_environment_constraints(self) -> Self:
        """Apply profile expectations and reject unsafe production config."""
        if self.environment == DeploymentEnvironment.TESTING:
            self.debug = False
            self.logging.json_output = False
            self.opentelemetry.enabled = False

        if self.environment == DeploymentEnvironment.PRODUCTION:
            if self.debug:
                raise ValueError("debug must be disabled in production")
            if not self.logging.json_output:
                raise ValueError("json_output must be enabled in production")

        return self

    @property
    def is_development(self) -> bool:
        """Return whether the active environment is development."""
        return self.environment == DeploymentEnvironment.DEVELOPMENT

    @property
    def is_testing(self) -> bool:
        """Return whether the active environment is testing."""
        return self.environment == DeploymentEnvironment.TESTING

    @property
    def is_production(self) -> bool:
        """Return whether the active environment is production."""
        return self.environment == DeploymentEnvironment.PRODUCTION


class DevelopmentSettings(AppSettings):
    """Development profile defaults."""

    __test__ = False

    environment: Literal[
        DeploymentEnvironment.DEVELOPMENT
    ] = DeploymentEnvironment.DEVELOPMENT
    debug: bool = True
    logging: LoggingSettings = Field(
        default_factory=lambda: LoggingSettings(
            level="DEBUG", json_output=False
        )
    )
    opentelemetry: OpenTelemetrySettings = Field(
        default_factory=lambda: OpenTelemetrySettings(
            enabled=True, trace_sample_ratio=1.0
        )
    )


class TestingSettings(AppSettings):
    """Testing profile defaults."""

    __test__ = False

    environment: Literal[
        DeploymentEnvironment.TESTING
    ] = DeploymentEnvironment.TESTING
    debug: bool = False
    logging: LoggingSettings = Field(
        default_factory=lambda: LoggingSettings(
            level="WARNING", json_output=False
        )
    )
    opentelemetry: OpenTelemetrySettings = Field(
        default_factory=lambda: OpenTelemetrySettings(
            enabled=False, trace_sample_ratio=0.0
        )
    )
    neo4j: Neo4jSettings = Field(
        default_factory=lambda: Neo4jSettings(enabled=False)
    )
    cosmos: CosmosSettings = Field(
        default_factory=lambda: CosmosSettings(enabled=False)
    )
    blob: BlobStorageSettings = Field(
        default_factory=lambda: BlobStorageSettings(enabled=False)
    )


class ProductionSettings(AppSettings):
    """Production profile defaults."""

    __test__ = False

    environment: Literal[
        DeploymentEnvironment.PRODUCTION
    ] = DeploymentEnvironment.PRODUCTION
    debug: bool = False
    logging: LoggingSettings = Field(
        default_factory=lambda: LoggingSettings(
            level="INFO", json_output=True
        )
    )
    opentelemetry: OpenTelemetrySettings = Field(
        default_factory=lambda: OpenTelemetrySettings(
            enabled=True, trace_sample_ratio=0.1
        )
    )


def _resolve_settings_class(
    environment: DeploymentEnvironment,
) -> type[AppSettings]:
    if environment == DeploymentEnvironment.DEVELOPMENT:
        return DevelopmentSettings
    if environment == DeploymentEnvironment.TESTING:
        return TestingSettings
    return ProductionSettings


def load_settings(
    *,
    env_file: str | Path | None = DEFAULT_ENV_FILE,
    secrets_dir: str | Path | None = None,
    **overrides: Any,
) -> AppSettings:
    """Load and validate a fresh application settings object."""
    resolved_secrets_dir = secrets_dir or os.getenv("SENTINEL_SECRETS_DIR")
    bootstrap = AppSettings(
        _env_file=env_file,
        _secrets_dir=resolved_secrets_dir,
        **overrides,
    )
    settings_cls = _resolve_settings_class(bootstrap.environment)
    if type(bootstrap) is settings_cls:
        return bootstrap
    return settings_cls(
        _env_file=env_file,
        _secrets_dir=resolved_secrets_dir,
        **overrides,
    )


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Return cached settings for dependency injection and startup."""
    return load_settings()


def reset_settings_cache() -> None:
    """Clear the cached settings instance, primarily for tests."""
    get_settings.cache_clear()
