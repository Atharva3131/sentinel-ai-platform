"""Azure Monitor / Application Insights MetricsEvidenceProvider.

Queries Azure Monitor (Application Insights) for metric observations during
incident investigation, providing Evidence items with structured metric data.

API used:
    GET https://api.applicationinsights.io/v1/apps/{app_id}/metrics/{metric_id}
        ?timespan=PT{window}S
        &aggregation=avg

Reference:
    https://learn.microsoft.com/en-us/rest/api/application-insights/metrics/get

Wire contract with MetricsEvidenceProvider protocol:
  kind           → EvidenceSourceKind.METRICS
  collect()      → list[Evidence] — one item per queried metric per service
  query_metric() → dict with raw metric data
  health_check() → bool

Evidence output:
  Each metric query result becomes one Evidence item. The metric value and
  metadata are stored in ``structured_data`` for downstream consumption.

Metric name mapping (domain-neutral → Application Insights metric IDs):
    error_rate              → requests/failed
    request_rate            → requests/count
    latency_ms              → requests/duration
    exception_rate          → exceptions/count
    dependency_failure_rate → dependencies/failed

Automatic metric queries:
  When no explicit metric_queries are supplied in context, the provider
  automatically queries these metrics for each affected service:
    - error_rate
    - request_rate
    - latency_ms
    - exception_rate
    - dependency_failure_rate

Authentication (in priority order):
  1. API key — ``x-api-key: <key>`` header (no AAD token needed).
  2. AAD Bearer token — obtained from the injected ``credential`` object via
     ``azure.identity.aio`` (Managed Identity or Service Principal).

Security:
  * API key is stored as SecretStr and never logged.
  * Base URL is logged; credentials are redacted.
  * Error responses are sanitized before logging.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from backend.configuration.settings import EvidenceProviderSettings
from backend.models.evidence import (
    Evidence,
    EvidenceSource,
    EvidenceSourceKind,
    EvidenceStatus,
)
from backend.models.incident import Incident
from backend.providers.evidence.base import EvidenceHttpBase

log = structlog.get_logger(__name__)

_PROVIDER_NAME = "azure_monitor"
_BASE_URL = "https://api.applicationinsights.io"
_API_VERSION = "v1"
_HEALTH_PATH = "/status"

# Metric name → Application Insights metric ID mapping
_METRIC_ID_MAP: dict[str, str] = {
    # Error / failure rates
    "error_rate": "requests/failed",
    "failure_rate": "requests/failed",
    # Request throughput
    "request_rate": "requests/count",
    "requests_per_second": "requests/count",
    # Latency
    "latency_ms": "requests/duration",
    "latency_p99": "requests/duration",
    "response_time_ms": "requests/duration",
    # Availability
    "availability": "availabilityResults/availabilityPercentage",
    # Exceptions
    "exception_rate": "exceptions/count",
    # Dependencies
    "dependency_failure_rate": "dependencies/failed",
    "dependency_duration_ms": "dependencies/duration",
}

# Default metrics to query per service when no explicit queries provided
_DEFAULT_METRICS = [
    "error_rate",
    "request_rate",
    "latency_ms",
    "exception_rate",
    "dependency_failure_rate",
]


@dataclass
class AzureMonitorMetricsProvider(EvidenceHttpBase):
    """Collects metric evidence from Azure Monitor / Application Insights.

    Inject via ``AzureMonitorMetricsProvider.from_settings(settings)``.
    """

    app_id: str = ""
    default_window_seconds: float = 300.0
    max_items: int = 20

    @classmethod
    def from_settings(
        cls, settings: EvidenceProviderSettings
    ) -> AzureMonitorMetricsProvider:
        """Factory: build a configured instance from EvidenceProviderSettings."""
        # App ID must be in the base_url or we extract from settings
        app_id = settings.base_url.split("/")[-1] if settings.base_url else ""
        if not app_id or app_id.startswith("http"):
            # Fall back: assume base_url is the full API endpoint
            app_id = ""

        provider = cls(
            provider_name=_PROVIDER_NAME,
            base_url=settings.base_url or _BASE_URL,
            app_id=app_id,
            timeout_seconds=settings.timeout_seconds,
            max_retries=settings.max_retries,
            retry_min_wait=settings.retry_min_wait_seconds,
            retry_max_wait=settings.retry_max_wait_seconds,
            default_window_seconds=settings.default_window_seconds,
            max_items=settings.max_items,
        )
        provider._configure_auth(
            api_key=settings.api_key.get_secret_value()
            if settings.api_key
            else None,
            username=settings.username,
            password=settings.password.get_secret_value()
            if settings.password
            else None,
        )
        return provider

    # ── EvidenceProvider protocol ─────────────────────────────────────────

    @property
    def kind(self) -> EvidenceSourceKind:
        return EvidenceSourceKind.METRICS

    @property
    def name(self) -> str:
        return _PROVIDER_NAME

    async def collect(
        self,
        incident: Incident,
        *,
        max_items: int = 20,
        context: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        """Collect metric evidence for all services affected by incident.

        The caller may pass ``context["metric_queries"]`` — a list of metric
        name strings (e.g. ["error_rate", "latency_ms"]) to query.  If absent,
        the provider automatically queries the default metrics for each service.

        Returns Evidence items with structured metric data.
        """
        ctx = context or {}
        queries: list[str] = list(ctx.get("metric_queries", _DEFAULT_METRICS))
        window = float(ctx.get("window_seconds", self.default_window_seconds))
        end_time = datetime.now(UTC)
        start_time = datetime.fromtimestamp(
            end_time.timestamp() - window, tz=UTC
        )

        bound_log = log.bind(
            provider=_PROVIDER_NAME,
            incident_id=incident.incident_id,
            correlation_id=incident.correlation_id,
        )

        evidence_items: list[Evidence] = []
        services = incident.affected_services[:5]  # Limit to top 5 services

        for service in services:
            for metric_name in queries[: max_items - len(evidence_items)]:
                try:
                    value = await self._fetch_metric(
                        app_id=self.app_id,
                        metric_id=_METRIC_ID_MAP.get(metric_name, metric_name),
                        metric_name=metric_name,
                        service=service,
                        start_time=start_time,
                        end_time=end_time,
                        window_seconds=window,
                        incident_id=incident.incident_id,
                        correlation_id=incident.correlation_id,
                    )
                except Exception as exc:
                    bound_log.warning(
                        "azure_monitor_metric_query_failed",
                        metric=metric_name,
                        service=service,
                        error=str(exc),
                    )
                    continue

                if value is not None:
                    item = _metric_to_evidence(
                        metric_name=metric_name,
                        service=service,
                        value=value,
                        incident=incident,
                        window_seconds=window,
                    )
                    evidence_items.append(item)

                if len(evidence_items) >= max_items:
                    break

            if len(evidence_items) >= max_items:
                break

        bound_log.info(
            "azure_monitor_metrics_collected",
            evidence_count=len(evidence_items),
            services_queried=len(services),
            metrics_per_service=len(queries),
        )
        return evidence_items

    async def query_metric(
        self,
        metric_name: str,
        service: str,
        *,
        window_seconds: float = 300.0,
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Query a specific metric for service within window_seconds.

        Returns raw Azure Monitor metric data.
        """
        end_time = datetime.now(UTC)
        start_time = datetime.fromtimestamp(
            end_time.timestamp() - window_seconds, tz=UTC
        )

        value = await self._fetch_metric(
            app_id=self.app_id,
            metric_id=_METRIC_ID_MAP.get(metric_name, metric_name),
            metric_name=metric_name,
            service=service,
            start_time=start_time,
            end_time=end_time,
            window_seconds=window_seconds,
        )

        return {
            "metric_name": metric_name,
            "service": service,
            "value": value,
            "window_seconds": window_seconds,
        }

    async def health_check(self) -> bool:
        """Return True when Azure Monitor is reachable."""
        try:
            await self._get(
                _HEALTH_PATH,
                span_name="evidence.azure_monitor.health",
            )
            return True
        except Exception:
            return False

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _fetch_metric(
        self,
        app_id: str,
        metric_id: str,
        metric_name: str,
        service: str,
        start_time: datetime,
        end_time: datetime,
        window_seconds: float,
        *,
        incident_id: str | None = None,
        correlation_id: str | None = None,
    ) -> float | None:
        """Fetch a single metric value from Azure Monitor."""
        # Build timespan in ISO 8601 format: PT{seconds}S
        timespan = f"PT{int(window_seconds)}S"

        # Build the metrics endpoint path
        path = f"/{_API_VERSION}/apps/{app_id}/metrics/{metric_id}"

        try:
            body = await self._get(
                path,
                params={
                    "timespan": timespan,
                    "aggregation": "avg",
                },
                span_name="evidence.azure_monitor.fetch_metric",
                incident_id=incident_id,
                correlation_id=correlation_id,
            )

            # Extract value from response
            # Azure Monitor returns structured data with metric values
            # Format: { "value": { "<metric_id>": { "sum": ..., "avg": ..., "count": ... } } }
            value_dict = body.get("value", {})
            if metric_id in value_dict:
                metric_data = value_dict[metric_id]
                return float(metric_data.get("avg", 0.0))
            return None
        except Exception as exc:
            log.warning(
                "azure_monitor_metric_fetch_error",
                metric=metric_name,
                metric_id=metric_id,
                service=service,
                error=str(exc),
            )
            return None


# ---------------------------------------------------------------------------
# Evidence construction
# ---------------------------------------------------------------------------


def _metric_to_evidence(
    metric_name: str,
    service: str,
    value: float,
    incident: Incident,
    window_seconds: float,
) -> Evidence:
    """Convert an Azure Monitor metric query result to an Evidence item."""
    now = datetime.now(UTC)

    # Build human-readable content
    content = (
        f"Metric '{metric_name}' for service '{service}': "
        f"value={value:.4g} (aggregated over {window_seconds:.0f}s)"
    )

    # Relevance scoring: always high for application metrics
    relevance = 0.8

    source = EvidenceSource(
        source_id=str(uuid.uuid4()),
        kind=EvidenceSourceKind.METRICS,
        name=_PROVIDER_NAME,
        collected_at=now,
        authoritative=True,
        metadata={"metric_name": metric_name, "service": service},
    )

    return Evidence(
        evidence_id=str(uuid.uuid4()),
        incident_id=incident.incident_id,
        source=source,
        title=f"Metrics: {metric_name} ({service})",
        content=content,
        status=EvidenceStatus.CONFIRMED,
        relevance_score=relevance,
        collected_at=now,
        structured_data={
            "service": service,
            "metric_name": metric_name,
            "value": value,
            "window_seconds": window_seconds,
        },
        tags=("azure_monitor", "metrics"),
    )
