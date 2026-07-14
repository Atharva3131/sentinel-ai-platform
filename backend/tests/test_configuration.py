"""Configuration system unit tests."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.configuration.settings import (
    DeploymentEnvironment,
    ProductionSettings,
    TestingSettings,
    get_settings,
    load_settings,
    reset_settings_cache,
)


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Generator[None, None, None]:
    """Keep the cached settings singleton isolated between tests."""
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_testing_profile_defaults_disable_runtime_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Testing settings should default to safe, lightweight dependencies."""
    monkeypatch.setenv("SENTINEL_ENVIRONMENT", "testing")

    settings = load_settings(env_file=None)

    assert isinstance(settings, TestingSettings)
    assert settings.environment == DeploymentEnvironment.TESTING
    assert settings.debug is False
    assert settings.opentelemetry.enabled is False
    assert settings.logging.json_output is False
    assert settings.neo4j.enabled is False
    assert settings.cosmos.enabled is False
    assert settings.blob.enabled is False


def test_secret_files_populate_nested_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Nested secret files should load with SENTINEL_ names and override defaults."""
    monkeypatch.setenv("SENTINEL_ENVIRONMENT", "production")
    monkeypatch.setenv("SENTINEL_LOGGING__JSON_OUTPUT", "true")
    secret_path = tmp_path / "SENTINEL_POSTGRES__PASSWORD"
    secret_path.write_text("from-secret-file\n", encoding="utf-8")

    settings = load_settings(env_file=None, secrets_dir=tmp_path)

    assert settings.postgres.password.get_secret_value() == "from-secret-file"


def test_production_rejects_debug_mode() -> None:
    """Production configuration must fail fast when debug is enabled."""
    with pytest.raises(ValidationError):
        ProductionSettings(debug=True)


def test_cached_settings_support_dependency_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DI entrypoint should return one stable settings instance."""
    monkeypatch.setenv("SENTINEL_ENVIRONMENT", "development")

    first = get_settings()
    second = get_settings()

    assert first is second


def test_client_secret_authentication_requires_complete_credential_set() -> None:
    """Partial Azure service principal settings must be rejected."""
    with pytest.raises(ValidationError):
        load_settings(
            env_file=None,
            environment="production",
            logging={"json_output": True},
            azure={
                "authentication_mode": "client_secret",
                "tenant_id": "tenant-only",
            },
        )


def test_typed_connection_urls_are_derived_from_credentials() -> None:
    """Structured connection settings should derive stable client URLs."""
    settings = load_settings(
        env_file=None,
        environment="development",
        postgres={
            "host": "db.internal",
            "port": 5432,
            "database": "sentinel",
            "username": "svc_user",
            "password": "pg-pass",
        },
        redis={
            "host": "cache.internal",
            "port": 6380,
            "database": 5,
            "password": "redis-pass",
            "ssl": True,
        },
    )

    assert settings.postgres.sqlalchemy_url == (
        "postgresql+asyncpg://svc_user:pg-pass@db.internal:5432/sentinel"
    )
    assert settings.redis.redis_url == "rediss://:redis-pass@cache.internal:6380/5"
