"""Typed, environment-backed application settings."""

from __future__ import annotations

from functools import cached_property
from typing import Literal, Self

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerSettings(BaseModel):
    """HTTP server settings."""

    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    workers: int = Field(default=1, ge=1)


class LoggingSettings(BaseModel):
    """Structured logging settings."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    json_output: bool = True


class ObservabilitySettings(BaseModel):
    """OpenTelemetry export settings."""

    enabled: bool = True
    otlp_http_endpoint: str | None = None
    trace_sample_ratio: float = Field(default=0.1, ge=0.0, le=1.0)
    exclude_health_endpoints: bool = True


class PostgresSettings(BaseModel):
    """PostgreSQL connection-pool settings."""

    url: SecretStr = SecretStr(
        "postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel"
    )
    pool_size: int = Field(default=10, ge=1)
    max_overflow: int = Field(default=20, ge=0)
    pool_timeout_seconds: float = Field(default=10.0, gt=0)
    pool_recycle_seconds: int = Field(default=1800, ge=60)


class RedisSettings(BaseModel):
    """Redis connection settings."""

    url: SecretStr = SecretStr("redis://localhost:6379/0")
    socket_timeout_seconds: float = Field(default=5.0, gt=0)
    socket_connect_timeout_seconds: float = Field(default=3.0, gt=0)
    max_connections: int = Field(default=100, ge=1)


class Neo4jSettings(BaseModel):
    """Neo4j driver settings."""

    enabled: bool = True
    uri: str = "neo4j://localhost:7687"
    username: str = "neo4j"
    password: SecretStr = SecretStr("sentinel-password")
    connection_timeout_seconds: float = Field(default=5.0, gt=0)
    max_connection_pool_size: int = Field(default=50, ge=1)


class CosmosSettings(BaseModel):
    """Azure Cosmos DB settings."""

    enabled: bool = False
    connection_string: SecretStr | None = None
    endpoint: str | None = None
    database_name: str = "sentinel"

    @model_validator(mode="after")
    def validate_endpoint(self) -> Self:
        """Require a connection source whenever Cosmos DB is enabled."""
        if self.enabled and not self.connection_string and not self.endpoint:
            raise ValueError("Cosmos DB requires connection_string or endpoint when enabled")
        return self


class BlobSettings(BaseModel):
    """Azure Blob Storage settings."""

    enabled: bool = False
    connection_string: SecretStr | None = None
    account_url: str | None = None

    @model_validator(mode="after")
    def validate_endpoint(self) -> Self:
        """Require a connection source whenever Blob Storage is enabled."""
        if self.enabled and not self.connection_string and not self.account_url:
            raise ValueError("Blob Storage requires connection_string or account_url when enabled")
        return self


class HealthSettings(BaseModel):
    """Health-probe execution settings."""

    dependency_timeout_seconds: float = Field(default=3.0, gt=0, le=30.0)


class AppSettings(BaseSettings):
    """Root application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SENTINEL_",
        env_nested_delimiter="__",
        env_ignore_empty=True,
        extra="ignore",
    )

    app_name: str = "sentinel-ai-platform"
    app_version: str = "0.1.0"
    environment: Literal["development", "test", "staging", "production"] = "development"
    debug: bool = False
    api_prefix: str = "/api/v1"
    server: ServerSettings = Field(default_factory=ServerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    cosmos: CosmosSettings = Field(default_factory=CosmosSettings)
    blob: BlobSettings = Field(default_factory=BlobSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)

    @model_validator(mode="after")
    def validate_production_safety(self) -> Self:
        """Reject unsafe debugging settings in production."""
        if self.environment == "production" and self.debug:
            raise ValueError("debug must be disabled in production")
        return self

    @cached_property
    def is_production(self) -> bool:
        """Return whether the active environment is production."""
        return self.environment == "production"


def load_settings() -> AppSettings:
    """Load and validate a fresh application settings object."""
    return AppSettings()
