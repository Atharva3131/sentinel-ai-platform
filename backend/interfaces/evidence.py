"""Evidence source provider interfaces.

Each interface follows the same three-point contract:
  1. ``kind`` property — identifies the source category
  2. ``collect()`` — async collection returning a list of Evidence
  3. ``health_check()`` — async liveness check

Provider implementations are injected; the investigation engine depends
only on these protocols, never on concrete provider classes.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from backend.models.evidence import Evidence, EvidenceSourceKind
from backend.models.incident import Incident


@runtime_checkable
class EvidenceProvider(Protocol):
    """Base protocol satisfied by every evidence source provider."""

    @property
    def kind(self) -> EvidenceSourceKind:
        """Identify the category of evidence this provider supplies."""
        ...

    @property
    def name(self) -> str:
        """Return a stable provider name for logging and provenance."""
        ...

    async def collect(
        self,
        incident: Incident,
        *,
        max_items: int = 20,
        context: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        """Collect evidence relevant to *incident*.

        Args:
            incident: The incident being investigated.
            max_items: Upper bound on returned evidence items.
            context: Optional provider-specific parameters.

        Returns:
            List of Evidence items ordered by descending relevance.
        """
        ...

    async def health_check(self) -> bool:
        """Return True when the provider is reachable and operational."""
        ...


@runtime_checkable
class MetricsEvidenceProvider(EvidenceProvider, Protocol):
    """Provider that queries time-series metrics systems (Prometheus, Datadog, etc.)."""

    async def query_metric(
        self,
        metric_name: str,
        service: str,
        *,
        window_seconds: float = 300.0,
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Query a specific metric for a service within a time window."""
        ...


@runtime_checkable
class LogsEvidenceProvider(EvidenceProvider, Protocol):
    """Provider that queries log aggregation systems (Elastic, Splunk, etc.)."""

    async def search_logs(
        self,
        service: str,
        query: str,
        *,
        window_seconds: float = 300.0,
        max_lines: int = 500,
    ) -> list[str]:
        """Search for log lines matching *query* within the window."""
        ...


@runtime_checkable
class TracesEvidenceProvider(EvidenceProvider, Protocol):
    """Provider that queries distributed tracing systems (Jaeger, Zipkin, OTLP, etc.)."""

    async def query_traces(
        self,
        service: str,
        *,
        window_seconds: float = 300.0,
        error_only: bool = False,
        max_traces: int = 50,
    ) -> list[dict[str, Any]]:
        """Return trace spans relevant to *service* in the time window."""
        ...


@runtime_checkable
class DeploymentEvidenceProvider(EvidenceProvider, Protocol):
    """Provider that reads deployment and change history."""

    async def recent_deployments(
        self,
        service: str,
        *,
        window_seconds: float = 86_400.0,
        max_deployments: int = 10,
    ) -> list[dict[str, Any]]:
        """Return recent deployments for *service* within the window."""
        ...


@runtime_checkable
class ConfigurationEvidenceProvider(EvidenceProvider, Protocol):
    """Provider that reads service configuration and its change history."""

    async def current_config(
        self,
        service: str,
        *,
        include_secrets: bool = False,
    ) -> dict[str, Any]:
        """Return the current effective configuration for *service*."""
        ...

    async def config_changes(
        self,
        service: str,
        *,
        window_seconds: float = 86_400.0,
    ) -> list[dict[str, Any]]:
        """Return configuration changes for *service* within the window."""
        ...


@runtime_checkable
class InfrastructureEvidenceProvider(EvidenceProvider, Protocol):
    """Provider that reads infrastructure state (CPU, memory, network, etc.)."""

    async def resource_utilization(
        self,
        service: str,
        *,
        window_seconds: float = 300.0,
    ) -> dict[str, float]:
        """Return resource utilization metrics for *service*."""
        ...


@runtime_checkable
class HistoricalIncidentsProvider(EvidenceProvider, Protocol):
    """Provider that queries the historical incident knowledge base."""

    async def similar_incidents(
        self,
        incident: Incident,
        *,
        max_results: int = 10,
        min_similarity: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Return historically similar incidents to *incident*."""
        ...


@runtime_checkable
class KnowledgeBaseProvider(EvidenceProvider, Protocol):
    """Provider that queries the runbook / knowledge-base retrieval system."""

    async def retrieve_runbooks(
        self,
        incident: Incident,
        *,
        max_results: int = 5,
    ) -> list[dict[str, Any]]:
        """Return relevant runbooks for *incident*."""
        ...
