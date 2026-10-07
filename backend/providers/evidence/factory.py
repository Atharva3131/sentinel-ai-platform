"""Evidence provider factory — builds configured providers from EvidenceSettings.

Returns fake (in-memory) providers when name=fake so no network calls are
made during local development and unit tests.
"""

from __future__ import annotations

from backend.configuration.settings import (
    EvidenceProviderName,
    EvidenceSettings,
)
from backend.interfaces.evidence import (
    LogsEvidenceProvider,
    MetricsEvidenceProvider,
    TracesEvidenceProvider,
)
from backend.interfaces.fake_evidence import (
    FakeLogsProvider,
    FakeMetricsProvider,
    FakeTracesProvider,
)
from backend.providers.evidence.azure_monitor import (
    AzureMonitorMetricsProvider,
)
from backend.providers.evidence.logs import ElasticLogsProvider
from backend.providers.evidence.metrics import PrometheusMetricsProvider
from backend.providers.evidence.traces import OTLPTracesProvider


def build_metrics_provider(settings: EvidenceSettings) -> MetricsEvidenceProvider:
    """Return the configured metrics evidence provider."""
    cfg = settings.metrics
    if cfg.name == EvidenceProviderName.FAKE or not cfg.enabled:
        return FakeMetricsProvider()
    if cfg.name == EvidenceProviderName.PROMETHEUS:
        return PrometheusMetricsProvider.from_settings(cfg)
    if cfg.name == EvidenceProviderName.AZURE_MONITOR:
        return AzureMonitorMetricsProvider.from_settings(cfg)
    raise ValueError(f"Unknown metrics provider: {cfg.name!r}")


def build_logs_provider(settings: EvidenceSettings) -> LogsEvidenceProvider:
    """Return the configured logs evidence provider."""
    cfg = settings.logs
    if cfg.name == EvidenceProviderName.FAKE or not cfg.enabled:
        return FakeLogsProvider()
    if cfg.name == EvidenceProviderName.ELASTIC:
        return ElasticLogsProvider.from_settings(cfg)
    raise ValueError(f"Unknown logs provider: {cfg.name!r}")


def build_traces_provider(settings: EvidenceSettings) -> TracesEvidenceProvider:
    """Return the configured traces evidence provider."""
    cfg = settings.traces
    if cfg.name == EvidenceProviderName.FAKE or not cfg.enabled:
        return FakeTracesProvider()
    if cfg.name == EvidenceProviderName.OTLP:
        return OTLPTracesProvider.from_settings(cfg)
    raise ValueError(f"Unknown traces provider: {cfg.name!r}")


def build_all_providers(
    settings: EvidenceSettings,
) -> tuple[MetricsEvidenceProvider, LogsEvidenceProvider, TracesEvidenceProvider]:
    """Build and return all three evidence providers from a single settings object."""
    return (
        build_metrics_provider(settings),
        build_logs_provider(settings),
        build_traces_provider(settings),
    )
