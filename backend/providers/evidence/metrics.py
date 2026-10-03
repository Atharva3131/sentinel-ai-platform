"""Prometheus-compatible MetricsEvidenceProvider.

Queries the Prometheus HTTP API (``/api/v1/query_range``) to collect time-series
metric observations for an incident investigation window.

Wire contract with MetricsEvidenceProvider protocol:
  kind           → EvidenceSourceKind.METRICS
  collect()      → list[Evidence] — one item per relevant metric series
  query_metric() → dict with raw series data
  health_check() → bool

Evidence output:
  Each Prometheus result series becomes one Evidence item.  The series labels
  are stored in ``structured_data`` without transformation so downstream
  consumers can reason about them without knowing about Prometheus.

The provider does NOT hard-code specific metric names.  The investigation
layer supplies queries via the ``context`` dict key ``"metric_queries"``,
which is a list of PromQL expression strings.  When no queries are supplied
the provider falls back to a safe per-service absent-metric placeholder to
signal that metric evidence was attempted but no queries were configured.

Security:
  * Credentials never appear in logs or OTel attributes.
  * ``base_url`` is logged; the API key header is not.
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

_PROVIDER_NAME = "prometheus"
_QUERY_RANGE_PATH = "/api/v1/query_range"
_HEALTH_PATH = "/api/v1/status/buildinfo"

# Step resolution as a fraction of the window
_DEFAULT_STEP_FRACTION = 0.01   # 1 % of window → ≈ 3 s for 5-min window
_MIN_STEP_SECONDS = 1.0
_MAX_STEP_SECONDS = 300.0


@dataclass
class PrometheusMetricsProvider(EvidenceHttpBase):
    """Collects metric evidence from a Prometheus-compatible endpoint.

    Inject via ``PrometheusMetricsProvider.from_settings(settings)``.
    """

    default_window_seconds: float = 300.0
    max_items: int = 20

    @classmethod
    def from_settings(cls, settings: EvidenceProviderSettings) -> PrometheusMetricsProvider:
        """Factory: build a configured instance from EvidenceProviderSettings."""
        provider = cls(
            provider_name=_PROVIDER_NAME,
            base_url=settings.base_url or "http://localhost:9090",
            timeout_seconds=settings.timeout_seconds,
            max_retries=settings.max_retries,
            retry_min_wait=settings.retry_min_wait_seconds,
            retry_max_wait=settings.retry_max_wait_seconds,
            default_window_seconds=settings.default_window_seconds,
            max_items=settings.max_items,
        )
        provider._configure_auth(
            api_key=settings.api_key.get_secret_value() if settings.api_key else None,
            username=settings.username,
            password=settings.password.get_secret_value() if settings.password else None,
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
        """Collect metric evidence for all services affected by *incident*.

        The caller may pass ``context["metric_queries"]`` — a list of PromQL
        expression strings to evaluate.  If absent, a generic per-service
        presence query is issued as a fallback.
        """
        ctx = context or {}
        queries: list[str] = list(ctx.get("metric_queries", []))
        window = float(ctx.get("window_seconds", self.default_window_seconds))
        end_ts = datetime.now(UTC).timestamp()
        start_ts = end_ts - window

        bound_log = log.bind(
            provider=_PROVIDER_NAME,
            incident_id=incident.incident_id,
            correlation_id=incident.correlation_id,
        )

        # If no explicit queries, generate a simple up-check per service
        if not queries:
            queries = [
                f'up{{job="{svc}"}}'
                for svc in incident.affected_services[:5]
            ]

        evidence_items: list[Evidence] = []
        for query in queries[: max_items]:
            try:
                series_list = await self._run_query_range(
                    query=query,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    window_seconds=window,
                    incident_id=incident.incident_id,
                    correlation_id=incident.correlation_id,
                )
            except Exception as exc:
                bound_log.warning(
                    "metrics_query_failed",
                    query=query,
                    error=str(exc),
                )
                continue

            for series in series_list[: max_items - len(evidence_items)]:
                item = _series_to_evidence(
                    series=series,
                    incident=incident,
                    query=query,
                )
                evidence_items.append(item)

            if len(evidence_items) >= max_items:
                break

        bound_log.info(
            "metrics_collected",
            evidence_count=len(evidence_items),
            queries_run=len(queries),
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
        """Query a specific metric for *service* within *window_seconds*.

        Returns raw Prometheus series data; does not produce Evidence objects.
        """
        label_str = ""
        if labels:
            pairs = ", ".join(f'{k}="{v}"' for k, v in labels.items())
            label_str = f"{{{pairs}}}"
        query = f'{metric_name}{{job="{service}"}}{label_str}'
        end_ts = datetime.now(UTC).timestamp()
        start_ts = end_ts - window_seconds

        series = await self._run_query_range(
            query=query,
            start_ts=start_ts,
            end_ts=end_ts,
            window_seconds=window_seconds,
        )
        return {"metric": metric_name, "service": service, "series": series}

    async def health_check(self) -> bool:
        """Return True when Prometheus is reachable."""
        try:
            await self._get(_HEALTH_PATH, span_name="evidence.prometheus.health")
            return True
        except Exception:
            return False

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _run_query_range(
        self,
        query: str,
        start_ts: float,
        end_ts: float,
        window_seconds: float,
        *,
        incident_id: str | None = None,
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        step = max(
            _MIN_STEP_SECONDS,
            min(_MAX_STEP_SECONDS, window_seconds * _DEFAULT_STEP_FRACTION),
        )
        body = await self._get(
            _QUERY_RANGE_PATH,
            params={
                "query": query,
                "start": start_ts,
                "end": end_ts,
                "step": f"{step:.0f}s",
            },
            span_name="evidence.prometheus.query_range",
            incident_id=incident_id,
            correlation_id=correlation_id,
        )
        if body.get("status") != "success":
            raise ValueError(
                f"Prometheus returned status={body.get('status')!r}: "
                f"{body.get('error', 'unknown error')}"
            )
        result: list[dict[str, Any]] = body.get("data", {}).get("result", [])
        return result

# ---------------------------------------------------------------------------
# Evidence construction
# ---------------------------------------------------------------------------


def _series_to_evidence(
    series: dict[str, Any],
    incident: Incident,
    query: str,
) -> Evidence:
    """Convert one Prometheus result series to an Evidence item."""
    now = datetime.now(UTC)
    metric_labels: dict[str, Any] = series.get("metric", {})
    values: list[Any] = series.get("values", [])

    metric_name = metric_labels.get("__name__", query.split("{")[0].strip())
    service = (
        metric_labels.get("job")
        or metric_labels.get("service")
        or metric_labels.get("instance", "unknown")
    )

    # Summarise values: last value + anomaly flag
    last_value: float | None = None
    min_val: float | None = None
    max_val: float | None = None
    if values:
        floats = []
        for _ts, v in values:
            try:
                floats.append(float(v))
            except (ValueError, TypeError):
                pass
        if floats:
            last_value = floats[-1]
            min_val = min(floats)
            max_val = max(floats)

    # Build human-readable content
    if last_value is not None:
        content = (
            f"Metric '{metric_name}' for service '{service}': "
            f"current={last_value:.4g}, min={min_val:.4g}, max={max_val:.4g} "
            f"over {len(values)} samples."
        )
    else:
        content = f"Metric '{metric_name}' returned no numeric values for service '{service}'."

    # Relevance: higher when the metric has samples and shows variation
    relevance = 0.6
    if values:
        relevance = 0.75
    if min_val is not None and max_val is not None and min_val != max_val:
        relevance = 0.85

    source = EvidenceSource(
        source_id=str(uuid.uuid4()),
        kind=EvidenceSourceKind.METRICS,
        name=_PROVIDER_NAME,
        collected_at=now,
        authoritative=True,
        metadata={"query": query},
    )

    return Evidence(
        evidence_id=str(uuid.uuid4()),
        incident_id=incident.incident_id,
        source=source,
        title=f"Metrics: {metric_name} ({service})",
        content=content,
        status=EvidenceStatus.CONFIRMED if values else EvidenceStatus.SUSPECTED,
        relevance_score=relevance,
        collected_at=now,
        structured_data={
            "service": service,
            "metric_name": metric_name,
            "labels": metric_labels,
            "last_value": last_value,
            "min_value": min_val,
            "max_value": max_val,
            "sample_count": len(values),
            "query": query,
        },
        tags=("prometheus", "metrics"),
    )
