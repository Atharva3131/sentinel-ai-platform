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
        password = self.password.get_secret_value() if self.password is not None else None
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
    connection_string: SecretStr | None = None
    preferred_locations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        """Require either a connection string or endpoint when Cosmos is enabled."""
        if self.enabled and not self.connection_string and not self.endpoint:
            raise ValueError(
                "cosmos requires connection_string or endpoint when the integration is enabled"
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
        """Require either a connection string or account URL when Blob Storage is enabled."""
        if self.enabled and not self.connection_string and not self.account_url:
            raise ValueError(
                "blob requires connection_string or account_url when the integration is enabled"
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


class AppSettings(BaseSettings):
    """Root application settings loaded from environment variables and secret files."""

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
    opentelemetry: OpenTelemetrySettings = Field(default_factory=OpenTelemetrySettings)
    azure: AzureCredentialSettings = Field(default_factory=AzureCredentialSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    cosmos: CosmosSettings = Field(default_factory=CosmosSettings)
    blob: BlobStorageSettings = Field(default_factory=BlobStorageSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    feature_flags: FeatureFlagSettings = Field(default_factory=FeatureFlagSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Load in order: explicit kwargs, environment, dotenv, then nested secret files."""
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
        """Apply profile expectations and reject unsafe production configuration."""
        if self.environment == DeploymentEnvironment.TESTING:
            self.debug = False
            self.logging.json_output = False
            self.opentelemetry.enabled = False

        if self.environment == DeploymentEnvironment.PRODUCTION:
            if self.debug:
                raise ValueError("debug must be disabled in production")
            if not self.logging.json_output:
                raise ValueError("logging.json_output must be enabled in production")

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

    environment: Literal[DeploymentEnvironment.DEVELOPMENT] = DeploymentEnvironment.DEVELOPMENT
    debug: bool = True
    logging: LoggingSettings = Field(
        default_factory=lambda: LoggingSettings(level="DEBUG", json_output=False)
    )
    opentelemetry: OpenTelemetrySettings = Field(
        default_factory=lambda: OpenTelemetrySettings(enabled=True, trace_sample_ratio=1.0)
    )


class TestingSettings(AppSettings):
    """Testing profile defaults."""

    __test__ = False

    environment: Literal[DeploymentEnvironment.TESTING] = DeploymentEnvironment.TESTING
    debug: bool = False
    logging: LoggingSettings = Field(
        default_factory=lambda: LoggingSettings(level="WARNING", json_output=False)
    )
    opentelemetry: OpenTelemetrySettings = Field(
        default_factory=lambda: OpenTelemetrySettings(enabled=False, trace_sample_ratio=0.0)
    )
    neo4j: Neo4jSettings = Field(default_factory=lambda: Neo4jSettings(enabled=False))
    cosmos: CosmosSettings = Field(default_factory=lambda: CosmosSettings(enabled=False))
    blob: BlobStorageSettings = Field(default_factory=lambda: BlobStorageSettings(enabled=False))


class ProductionSettings(AppSettings):
    """Production profile defaults."""

    __test__ = False

    environment: Literal[DeploymentEnvironment.PRODUCTION] = DeploymentEnvironment.PRODUCTION
    debug: bool = False
    logging: LoggingSettings = Field(
        default_factory=lambda: LoggingSettings(level="INFO", json_output=True)
    )
    opentelemetry: OpenTelemetrySettings = Field(
        default_factory=lambda: OpenTelemetrySettings(enabled=True, trace_sample_ratio=0.1)
    )


def _resolve_settings_class(environment: DeploymentEnvironment) -> type[AppSettings]:
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
    """Return cached settings for dependency injection and application startup."""
    return load_settings()


def reset_settings_cache() -> None:
    """Clear the cached settings instance, primarily for tests."""
    get_settings.cache_clear()
