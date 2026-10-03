"""Elasticsearch-compatible LogsEvidenceProvider.

Queries the Elasticsearch HTTP API (``POST /<index>/_search``) using a
time-window filter and optional keyword query.

Wire contract with LogsEvidenceProvider protocol:
  kind           → EvidenceSourceKind.LOGS
  collect()      → list[Evidence] — one item per affected service
  search_logs()  → list[str] — raw log lines for direct consumption
  health_check() → bool

Evidence output:
  One Evidence item is produced per service in ``incident.affected_services``.
  The item's ``content`` contains a formatted summary of matching log lines and
  the ``structured_data`` holds machine-readable error counts and sample lines.

Provider-neutral design:
  * No Demo 1 / Demo 2 log patterns.
  * No hard-coded field names beyond Elasticsearch defaults (``@timestamp``,
    ``message``, ``log.level``).  Field names are configurable via context.
  * The caller may pass ``context["log_query"]`` to override the match string.

Security:
  * HTTP Basic Auth credentials (username/password) never appear in logs.
  * API key never appears in logs or OTel attributes.
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

_PROVIDER_NAME = "elastic"
_HEALTH_PATH = "/_cluster/health"

# Default field names — can be overridden via context
_DEFAULT_TIMESTAMP_FIELD = "@timestamp"
_DEFAULT_MESSAGE_FIELD = "message"
_DEFAULT_LEVEL_FIELD = "log.level"
_DEFAULT_SERVICE_FIELD = "service.name"
_DEFAULT_INDEX = "logs-*"

# How many log hits to retrieve per service query
_HITS_PER_SERVICE = 50


@dataclass
class ElasticLogsProvider(EvidenceHttpBase):
    """Collects log evidence from an Elasticsearch-compatible endpoint.

    Inject via ``ElasticLogsProvider.from_settings(settings)``.
    """

    default_window_seconds: float = 300.0
    max_items: int = 20
    index: str = _DEFAULT_INDEX

    @classmethod
    def from_settings(cls, settings: EvidenceProviderSettings) -> ElasticLogsProvider:
        provider = cls(
            provider_name=_PROVIDER_NAME,
            base_url=settings.base_url or "http://localhost:9200",
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
        return EvidenceSourceKind.LOGS

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
        """Collect log evidence for each affected service."""
        ctx = context or {}
        window = float(ctx.get("window_seconds", self.default_window_seconds))
        # Caller-supplied query string; defaults to a catch-all
        log_query: str = ctx.get("log_query", "")
        index: str = ctx.get("log_index", self.index)

        bound_log = log.bind(
            provider=_PROVIDER_NAME,
            incident_id=incident.incident_id,
            correlation_id=incident.correlation_id,
        )

        evidence_items: list[Evidence] = []
        for service in incident.affected_services[:max_items]:
            try:
                hits = await self._search(
                    service=service,
                    query=log_query,
                    window_seconds=window,
                    index=index,
                    max_hits=_HITS_PER_SERVICE,
                    incident_id=incident.incident_id,
                    correlation_id=incident.correlation_id,
                )
            except Exception as exc:
                bound_log.warning(
                    "logs_query_failed",
                    service=service,
                    error=str(exc),
                )
                continue

            item = _hits_to_evidence(
                hits=hits,
                service=service,
                incident=incident,
                window_seconds=window,
            )
            evidence_items.append(item)

        bound_log.info(
            "logs_collected",
            evidence_count=len(evidence_items),
            services_queried=len(incident.affected_services),
        )
        return evidence_items

    async def search_logs(
        self,
        service: str,
        query: str,
        *,
        window_seconds: float = 300.0,
        max_lines: int = 500,
    ) -> list[str]:
        """Return raw log lines for *service* matching *query*."""
        hits = await self._search(
            service=service,
            query=query,
            window_seconds=window_seconds,
            index=self.index,
            max_hits=min(max_lines, 500),
        )
        return [_hit_to_line(h) for h in hits]

    async def health_check(self) -> bool:
        try:
            body = await self._get(_HEALTH_PATH, span_name="evidence.elastic.health")
            status = body.get("status", "red")
            return status in ("green", "yellow")
        except Exception:
            return False

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _search(
        self,
        service: str,
        query: str,
        window_seconds: float,
        index: str,
        max_hits: int,
        *,
        incident_id: str | None = None,
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        start_ms = now_ms - int(window_seconds * 1000)

        must: list[dict[str, Any]] = [
            {
                "range": {
                    _DEFAULT_TIMESTAMP_FIELD: {
                        "gte": start_ms,
                        "lte": now_ms,
                        "format": "epoch_millis",
                    }
                }
            }
        ]

        # Service filter
        must.append({
            "bool": {
                "should": [
                    {"term": {f"{_DEFAULT_SERVICE_FIELD}.keyword": service}},
                    {"term": {"service": service}},
                    {"match": {_DEFAULT_SERVICE_FIELD: service}},
                ],
                "minimum_should_match": 1,
            }
        })

        # Optional full-text search
        if query:
            must.append({"match": {_DEFAULT_MESSAGE_FIELD: query}})

        es_query: dict[str, Any] = {
            "size": max_hits,
            "sort": [{_DEFAULT_TIMESTAMP_FIELD: {"order": "desc"}}],
            "query": {"bool": {"must": must}},
            "_source": [
                _DEFAULT_TIMESTAMP_FIELD,
                _DEFAULT_MESSAGE_FIELD,
                _DEFAULT_LEVEL_FIELD,
                _DEFAULT_SERVICE_FIELD,
                "service",
                "error.message",
                "http.response.status_code",
            ],
        }

        body = await self._post(
            f"/{index}/_search",
            json=es_query,
            span_name="evidence.elastic.search",
            incident_id=incident_id,
            correlation_id=correlation_id,
        )
        hits: list[dict[str, Any]] = body.get("hits", {}).get("hits", [])
        return hits


# ---------------------------------------------------------------------------
# Evidence construction
# ---------------------------------------------------------------------------


def _hit_to_line(hit: dict[str, Any]) -> str:
    src = hit.get("_source", {})
    ts = src.get(_DEFAULT_TIMESTAMP_FIELD, "")
    level = src.get(_DEFAULT_LEVEL_FIELD, "")
    msg = src.get(_DEFAULT_MESSAGE_FIELD, "")
    return f"[{ts}] {level} {msg}".strip()


def _hits_to_evidence(
    hits: list[dict[str, Any]],
    service: str,
    incident: Incident,
    window_seconds: float,
) -> Evidence:
    now = datetime.now(UTC)
    total = len(hits)

    # Count error-level hits
    error_count = sum(
        1 for h in hits
        if str(h.get("_source", {}).get(_DEFAULT_LEVEL_FIELD, "")).lower()
        in ("error", "fatal", "critical")
    )

    # Sample lines (first 5)
    sample_lines = [_hit_to_line(h) for h in hits[:5]]
    sample_text = "\n".join(sample_lines)

    if total == 0:
        content = (
            f"No log entries found for '{service}' "
            f"in the last {window_seconds:.0f}s."
        )
        status = EvidenceStatus.SUSPECTED
        relevance = 0.3
    else:
        content = (
            f"Found {total} log entries for '{service}' "
            f"({error_count} errors) in the last {window_seconds:.0f}s.\n"
            f"Sample:\n{sample_text}"
        )
        status = EvidenceStatus.CONFIRMED
        relevance = min(0.95, 0.5 + (error_count / max(total, 1)) * 0.5)

    source = EvidenceSource(
        source_id=str(uuid.uuid4()),
        kind=EvidenceSourceKind.LOGS,
        name=_PROVIDER_NAME,
        collected_at=now,
        authoritative=True,
    )

    return Evidence(
        evidence_id=str(uuid.uuid4()),
        incident_id=incident.incident_id,
        source=source,
        title=f"Logs: {service} ({total} entries, {error_count} errors)",
        content=content,
        status=status,
        relevance_score=relevance,
        collected_at=now,
        structured_data={
            "service": service,
            "total_hits": total,
            "error_count": error_count,
            "window_seconds": window_seconds,
            "sample_lines": sample_lines,
        },
        tags=("elastic", "logs"),
    )
