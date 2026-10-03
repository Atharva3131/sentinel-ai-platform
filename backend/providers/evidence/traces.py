"""OTLP/Jaeger-compatible TracesEvidenceProvider.

Queries the Jaeger HTTP API (``GET /api/traces``) to collect distributed
trace evidence for an incident investigation window.  The Jaeger HTTP API
is the most widely deployed trace query interface and is compatible with
any OTLP-based tracing backend that ships with a Jaeger-compatible UI/API.

Wire contract with TracesEvidenceProvider protocol:
  kind           → EvidenceSourceKind.TRACES
  collect()      → list[Evidence] — one item per affected service
  query_traces() → list[dict] — raw span data
  health_check() → bool

Evidence output:
  One Evidence item is produced per affected service.  Root spans and error
  spans are extracted and summarised into human-readable content.  Full span
  data is stored in ``structured_data`` without Jaeger-specific coupling to the
  domain model.

Provider-neutral design:
  * No hard-coded trace field names beyond the Jaeger API response schema.
  * No Demo 1 / Demo 2 trace patterns.
  * ``context["error_only"]`` and ``context["max_traces"]`` allow the caller
    to tune the query.

Security:
  * Credentials never appear in logs or OTel attributes.
  * Bearer tokens are passed in Authorization header only.
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

_PROVIDER_NAME = "otlp"
_JAEGER_TRACES_PATH = "/api/traces"
_JAEGER_SERVICES_PATH = "/api/services"

# Jaeger query parameters
_DEFAULT_MAX_TRACES = 20
_DEFAULT_LIMIT = 20


@dataclass
class OTLPTracesProvider(EvidenceHttpBase):
    """Collects trace evidence from a Jaeger-compatible HTTP API.

    Inject via ``OTLPTracesProvider.from_settings(settings)``.
    """

    default_window_seconds: float = 300.0
    max_items: int = 20

    @classmethod
    def from_settings(cls, settings: EvidenceProviderSettings) -> OTLPTracesProvider:
        provider = cls(
            provider_name=_PROVIDER_NAME,
            base_url=settings.base_url or "http://localhost:16686",
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
        return EvidenceSourceKind.TRACES

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
        """Collect trace evidence for each affected service."""
        ctx = context or {}
        window = float(ctx.get("window_seconds", self.default_window_seconds))
        error_only = bool(ctx.get("error_only", False))
        max_traces = int(ctx.get("max_traces", _DEFAULT_MAX_TRACES))

        bound_log = log.bind(
            provider=_PROVIDER_NAME,
            incident_id=incident.incident_id,
            correlation_id=incident.correlation_id,
        )

        evidence_items: list[Evidence] = []
        for service in incident.affected_services[:max_items]:
            try:
                spans = await self._fetch_traces(
                    service=service,
                    window_seconds=window,
                    error_only=error_only,
                    max_traces=max_traces,
                    incident_id=incident.incident_id,
                    correlation_id=incident.correlation_id,
                )
            except Exception as exc:
                bound_log.warning(
                    "traces_query_failed",
                    service=service,
                    error=str(exc),
                )
                continue

            item = _spans_to_evidence(
                spans=spans,
                service=service,
                incident=incident,
                window_seconds=window,
                error_only=error_only,
            )
            evidence_items.append(item)

        bound_log.info(
            "traces_collected",
            evidence_count=len(evidence_items),
            services_queried=len(incident.affected_services),
        )
        return evidence_items

    async def query_traces(
        self,
        service: str,
        *,
        window_seconds: float = 300.0,
        error_only: bool = False,
        max_traces: int = 50,
    ) -> list[dict[str, Any]]:
        """Return raw span data for *service* in the time window."""
        return await self._fetch_traces(
            service=service,
            window_seconds=window_seconds,
            error_only=error_only,
            max_traces=max_traces,
        )

    async def health_check(self) -> bool:
        try:
            await self._get(
                _JAEGER_SERVICES_PATH,
                span_name="evidence.otlp.health",
            )
            return True
        except Exception:
            return False

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _fetch_traces(
        self,
        service: str,
        window_seconds: float,
        error_only: bool,
        max_traces: int,
        *,
        incident_id: str | None = None,
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        now_us = int(datetime.now(UTC).timestamp() * 1_000_000)   # microseconds
        start_us = now_us - int(window_seconds * 1_000_000)

        params: dict[str, Any] = {
            "service": service,
            "start": start_us,
            "end": now_us,
            "limit": min(max_traces, _DEFAULT_LIMIT),
        }
        if error_only:
            params["tags"] = 'error=true'

        body = await self._get(
            _JAEGER_TRACES_PATH,
            params=params,
            span_name="evidence.otlp.query_traces",
            incident_id=incident_id,
            correlation_id=correlation_id,
        )
        traces: list[dict[str, Any]] = body.get("data", [])
        return traces


# ---------------------------------------------------------------------------
# Evidence construction
# ---------------------------------------------------------------------------


def _spans_to_evidence(
    spans: list[dict[str, Any]],
    service: str,
    incident: Incident,
    window_seconds: float,
    error_only: bool,
) -> Evidence:
    now = datetime.now(UTC)
    total_traces = len(spans)

    # Count traces with error tags
    error_traces = 0
    slow_traces = 0
    duration_samples: list[float] = []

    for trace_data in spans:
        # Jaeger trace object has a list of spans under "spans"
        trace_spans: list[dict[str, Any]] = trace_data.get("spans", [trace_data])
        for span in trace_spans:
            tags: list[dict[str, Any]] = span.get("tags", [])
            has_error = any(
                t.get("key") == "error" and t.get("value") is True
                for t in tags
            )
            if has_error:
                error_traces += 1
            duration_us = span.get("duration", 0)
            if isinstance(duration_us, (int, float)):
                duration_ms = duration_us / 1000.0
                duration_samples.append(duration_ms)
                if duration_ms > 1000:  # > 1 second is "slow"
                    slow_traces += 1

    avg_duration_ms: float | None = None
    if duration_samples:
        avg_duration_ms = sum(duration_samples) / len(duration_samples)

    if total_traces == 0:
        content = (
            f"No traces found for '{service}' "
            f"in the last {window_seconds:.0f}s."
        )
        status = EvidenceStatus.SUSPECTED
        relevance = 0.3
    else:
        duration_str = (
            f", avg duration {avg_duration_ms:.0f}ms" if avg_duration_ms else ""
        )
        content = (
            f"Found {total_traces} trace(s) for '{service}' "
            f"({error_traces} with errors, {slow_traces} slow{duration_str}) "
            f"in the last {window_seconds:.0f}s."
        )
        status = EvidenceStatus.CONFIRMED
        relevance = min(0.95, 0.5 + (error_traces / max(total_traces, 1)) * 0.45)

    source = EvidenceSource(
        source_id=str(uuid.uuid4()),
        kind=EvidenceSourceKind.TRACES,
        name=_PROVIDER_NAME,
        collected_at=now,
        authoritative=True,
    )

    return Evidence(
        evidence_id=str(uuid.uuid4()),
        incident_id=incident.incident_id,
        source=source,
        title=(
            f"Traces: {service} "
            f"({total_traces} traces, {error_traces} errors)"
        ),
        content=content,
        status=status,
        relevance_score=relevance,
        collected_at=now,
        structured_data={
            "service": service,
            "total_traces": total_traces,
            "error_traces": error_traces,
            "slow_traces": slow_traces,
            "avg_duration_ms": avg_duration_ms,
            "window_seconds": window_seconds,
            "error_only_query": error_only,
        },
        tags=("otlp", "traces"),
    )
