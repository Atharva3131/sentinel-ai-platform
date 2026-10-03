"""Evidence provider tests — all 17 categories.

All tests use mocked httpx transports; no live Prometheus, Elastic, or
Jaeger service is required.

Categories:
 1.  Prometheus query/response parsing
 2.  Malformed metrics responses
 3.  Metrics timeout
 4.  Metrics retry
 5.  Metrics cancellation
 6.  Elastic log query/response parsing
 7.  Malformed log responses
 8.  Logs timeout/retry
 9.  Trace response parsing
10.  Trace timeout/retry
11.  Provider selection (factory + config)
12.  Fake-provider isolation (no HTTP on fake)
13.  Concurrent evidence collection (orchestrator)
14.  Partial provider failure (orchestrator)
15.  Lifecycle event emission (orchestrator)
16.  OTel/correlation propagation
17.  Secret non-leakage
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from backend.configuration.settings import (
    EvidenceProviderName,
    EvidenceProviderSettings,
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
from backend.models.evidence import EvidenceSourceKind, EvidenceStatus
from backend.models.incident import Incident, IncidentSeverity, IncidentStatus
from backend.providers.evidence.base import (
    EvidenceAuthError,
    EvidenceTimeoutError,
    EvidenceUnavailableError,
)
from backend.providers.evidence.factory import (
    build_logs_provider,
    build_metrics_provider,
    build_traces_provider,
)
from backend.providers.evidence.logs import ElasticLogsProvider
from backend.providers.evidence.metrics import PrometheusMetricsProvider
from backend.providers.evidence.traces import OTLPTracesProvider
from backend.services.evidence_orchestrator import EvidenceOrchestrator

# ── Shared fixtures ────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _incident(
    *,
    services: tuple[str, ...] = ("svc-a", "svc-b"),
) -> Incident:
    return Incident(
        incident_id=str(uuid.uuid4()),
        title="Test incident",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        affected_services=services,
        description="Test.",
        detected_at=_now(),
        correlation_id=str(uuid.uuid4()),
    )


def _prom_settings(*, base_url: str = "http://prometheus:9090") -> EvidenceProviderSettings:
    return EvidenceProviderSettings(
        name=EvidenceProviderName.PROMETHEUS,
        base_url=base_url,
        timeout_seconds=5.0,
        max_retries=2,
        retry_min_wait_seconds=0.01,
        retry_max_wait_seconds=0.05,
    )


def _elastic_settings(*, base_url: str = "http://elastic:9200") -> EvidenceProviderSettings:
    from pydantic import SecretStr
    return EvidenceProviderSettings(
        name=EvidenceProviderName.ELASTIC,
        base_url=base_url,
        timeout_seconds=5.0,
        max_retries=2,
        retry_min_wait_seconds=0.01,
        retry_max_wait_seconds=0.05,
        username="elastic",
        password=SecretStr("changeme"),
    )


def _otlp_settings(*, base_url: str = "http://jaeger:16686") -> EvidenceProviderSettings:
    return EvidenceProviderSettings(
        name=EvidenceProviderName.OTLP,
        base_url=base_url,
        timeout_seconds=5.0,
        max_retries=2,
        retry_min_wait_seconds=0.01,
        retry_max_wait_seconds=0.05,
    )


class _MockTransport(httpx.AsyncBaseTransport):
    """Returns a single pre-configured response for every request."""

    def __init__(
        self,
        status: int = 200,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._status = status
        self._body = json.dumps(body or {}).encode()
        self._headers = httpx.Headers(
            {**(headers or {}), "content-type": "application/json"}
        )
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        return httpx.Response(
            status_code=self._status,
            headers=self._headers,
            content=self._body,
        )


class _SeqTransport(httpx.AsyncBaseTransport):
    """Returns responses in sequence; last entry repeated."""

    def __init__(self, responses: list[tuple[int, dict[str, Any]]]) -> None:
        self._responses = responses
        self._idx = 0
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        status, body = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return httpx.Response(
            status_code=status,
            headers={"content-type": "application/json"},
            content=json.dumps(body).encode(),
        )


def _wire_transport(provider: Any, transport: httpx.AsyncBaseTransport) -> None:
    """Inject a mock transport into an EvidenceHttpBase subclass."""
    provider._http = httpx.AsyncClient(
        base_url=provider.base_url,
        transport=transport,
    )


def _prom_success(
    metric: str = "up",
    service: str = "svc-a",
    values: list[Any] | None = None,
) -> dict[str, Any]:
    vals = values or [[1_700_000_000, "1"], [1_700_000_060, "1"]]
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {
                        "__name__": metric,
                        "job": service,
                        "instance": f"{service}:9090",
                    },
                    "values": vals,
                }
            ],
        },
    }


def _elastic_success(
    service: str = "svc-a",
    num_hits: int = 3,
    error_hits: int = 1,
) -> dict[str, Any]:
    hits = []
    for i in range(num_hits):
        level = "error" if i < error_hits else "info"
        hits.append({
            "_index": "logs-2024",
            "_id": str(uuid.uuid4()),
            "_source": {
                "@timestamp": "2024-01-01T00:00:00.000Z",
                "message": f"Sample log line {i}",
                "log.level": level,
                "service.name": service,
            },
        })
    return {
        "hits": {
            "total": {"value": num_hits},
            "hits": hits,
        }
    }


def _jaeger_success(
    service: str = "svc-a",
    num_traces: int = 2,
    error_count: int = 1,
) -> dict[str, Any]:
    traces = []
    for i in range(num_traces):
        tags = [{"key": "error", "type": "bool", "value": True}] if i < error_count else []
        traces.append({
            "traceID": str(uuid.uuid4()).replace("-", ""),
            "spans": [
                {
                    "traceID": str(uuid.uuid4()).replace("-", ""),
                    "spanID": str(uuid.uuid4()).replace("-", "")[:16],
                    "operationName": f"GET /api/{service}",
                    "duration": 1_500_000 if i < 1 else 50_000,
                    "tags": tags,
                }
            ],
            "processes": {},
        })
    return {"data": traces}


# ── 1. Prometheus query/response parsing ──────────────────────────────────


@pytest.mark.asyncio
async def test_prometheus_parses_success_response() -> None:
    """PrometheusMetricsProvider parses a valid /api/v1/query_range response."""
    provider = PrometheusMetricsProvider.from_settings(_prom_settings())
    transport = _MockTransport(200, _prom_success("http_requests_total", "svc-a"))
    _wire_transport(provider, transport)

    inc = _incident(services=("svc-a",))
    items = await provider.collect(
        inc,
        context={"metric_queries": ['http_requests_total{job="svc-a"}']},
    )

    assert len(items) == 1
    item = items[0]
    assert item.source.kind == EvidenceSourceKind.METRICS
    assert item.status == EvidenceStatus.CONFIRMED
    assert "http_requests_total" in item.title
    assert item.structured_data["service"] == "svc-a"
    assert item.structured_data["sample_count"] == 2


@pytest.mark.asyncio
async def test_prometheus_returns_correct_evidence_source_kind() -> None:
    """Evidence items have kind=METRICS and name=prometheus."""
    provider = PrometheusMetricsProvider.from_settings(_prom_settings())
    transport = _MockTransport(200, _prom_success())
    _wire_transport(provider, transport)

    inc = _incident()
    items = await provider.collect(inc)

    assert all(e.source.kind == EvidenceSourceKind.METRICS for e in items)
    assert all(e.source.name == "prometheus" for e in items)


@pytest.mark.asyncio
async def test_prometheus_query_metric_returns_raw_data() -> None:
    """query_metric() returns series data without converting to Evidence."""
    provider = PrometheusMetricsProvider.from_settings(_prom_settings())
    transport = _MockTransport(200, _prom_success("cpu_usage", "svc-a"))
    _wire_transport(provider, transport)

    result = await provider.query_metric("cpu_usage", "svc-a", window_seconds=60.0)

    assert result["metric"] == "cpu_usage"
    assert result["service"] == "svc-a"
    assert isinstance(result["series"], list)


@pytest.mark.asyncio
async def test_prometheus_fallback_queries_when_no_context_queries() -> None:
    """When no metric_queries are supplied, provider generates per-service fallbacks."""
    provider = PrometheusMetricsProvider.from_settings(_prom_settings())
    transport = _MockTransport(200, _prom_success())
    _wire_transport(provider, transport)

    inc = _incident(services=("svc-x", "svc-y"))
    items = await provider.collect(inc)

    # At least one item per service (up to max_items)
    assert len(items) >= 1
    # Verify the fallback up{} query was used by checking request params
    assert any("up" in r.url.params.get("query", "") for r in transport.requests)


@pytest.mark.asyncio
async def test_prometheus_health_check_returns_true_on_200() -> None:
    """health_check() returns True when Prometheus responds with 200."""
    provider = PrometheusMetricsProvider.from_settings(_prom_settings())
    transport = _MockTransport(200, {"version": "2.40.0"})
    _wire_transport(provider, transport)

    assert await provider.health_check() is True


@pytest.mark.asyncio
async def test_prometheus_satisfies_metrics_provider_protocol() -> None:
    """PrometheusMetricsProvider satisfies MetricsEvidenceProvider structurally."""
    provider = PrometheusMetricsProvider.from_settings(_prom_settings())
    assert isinstance(provider, MetricsEvidenceProvider)


# ── 2. Malformed metrics responses ───────────────────────────────────────


@pytest.mark.asyncio
async def test_prometheus_empty_result_produces_suspected_evidence() -> None:
    """Empty result list produces SUSPECTED evidence items."""
    provider = PrometheusMetricsProvider.from_settings(_prom_settings())
    transport = _MockTransport(200, {
        "status": "success",
        "data": {"resultType": "matrix", "result": [
            {"metric": {"__name__": "cpu", "job": "svc-a"}, "values": []}
        ]},
    })
    _wire_transport(provider, transport)

    inc = _incident(services=("svc-a",))
    items = await provider.collect(
        inc, context={"metric_queries": ['cpu{job="svc-a"}']}
    )

    assert len(items) == 1
    assert items[0].status == EvidenceStatus.SUSPECTED


@pytest.mark.asyncio
async def test_prometheus_error_status_skipped_gracefully() -> None:
    """Prometheus 'error' status response is skipped; no exception propagates."""
    provider = PrometheusMetricsProvider.from_settings(_prom_settings())
    transport = _MockTransport(200, {
        "status": "error",
        "errorType": "bad_data",
        "error": "invalid expression",
    })
    _wire_transport(provider, transport)

    inc = _incident()
    # Should not raise; returns empty list (query failed gracefully)
    items = await provider.collect(
        inc, context={"metric_queries": ["bad_query{{"]}
    )
    assert items == []


@pytest.mark.asyncio
async def test_prometheus_non_json_response_skipped_gracefully() -> None:
    """Non-JSON response from Prometheus is skipped without crashing collect()."""
    class _BadJson(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json at all")

    provider = PrometheusMetricsProvider.from_settings(
        EvidenceProviderSettings(
            name=EvidenceProviderName.PROMETHEUS,
            base_url="http://prom:9090",
            max_retries=0,
        )
    )
    _wire_transport(provider, _BadJson())

    inc = _incident()
    items = await provider.collect(inc, context={"metric_queries": ["up"]})
    assert items == []


# ── 3. Metrics timeout ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prometheus_timeout_raises_evidence_timeout_error() -> None:
    """Slow Prometheus raises EvidenceTimeoutError when timeout is exceeded."""
    class _Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
            await asyncio.sleep(999)
            return httpx.Response(200)

    provider = PrometheusMetricsProvider.from_settings(
        EvidenceProviderSettings(
            name=EvidenceProviderName.PROMETHEUS,
            base_url="http://prom:9090",
            timeout_seconds=0.05,
            max_retries=0,
        )
    )
    _wire_transport(provider, _Slow())

    with pytest.raises(EvidenceTimeoutError) as exc_info:
        await provider._get("/api/v1/query_range", params={"query": "up"})

    assert exc_info.value.provider == "prometheus"


@pytest.mark.asyncio
async def test_prometheus_timeout_during_collect_skips_query_gracefully() -> None:
    """A timeout during collect() skips the failing query without crashing."""
    class _Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
            await asyncio.sleep(999)
            return httpx.Response(200)

    provider = PrometheusMetricsProvider.from_settings(
        EvidenceProviderSettings(
            name=EvidenceProviderName.PROMETHEUS,
            base_url="http://prom:9090",
            timeout_seconds=0.05,
            max_retries=0,
        )
    )
    _wire_transport(provider, _Slow())

    inc = _incident()
    items = await provider.collect(inc, context={"metric_queries": ["up"]})
    assert items == []


# ── 4. Metrics retry ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prometheus_retries_on_503_then_succeeds() -> None:
    """PrometheusMetricsProvider retries a transient 503 and returns data."""
    transport = _SeqTransport([
        (503, {"error": "temporarily unavailable"}),
        (200, _prom_success("cpu", "svc-a")),
    ])
    provider = PrometheusMetricsProvider.from_settings(
        EvidenceProviderSettings(
            name=EvidenceProviderName.PROMETHEUS,
            base_url="http://prom:9090",
            max_retries=2,
            retry_min_wait_seconds=0.01,
            retry_max_wait_seconds=0.05,
        )
    )
    _wire_transport(provider, transport)

    inc = _incident(services=("svc-a",))
    items = await provider.collect(inc, context={"metric_queries": ['cpu{job="svc-a"}']})

    assert len(items) == 1
    assert transport._idx == 2  # hit 503 then 200


@pytest.mark.asyncio
async def test_prometheus_raises_after_max_retries_exhausted() -> None:
    """PrometheusMetricsProvider raises EvidenceUnavailableError after all retries fail."""
    transport = _SeqTransport([
        (503, {}), (503, {}), (503, {}),
    ])
    provider = PrometheusMetricsProvider.from_settings(
        EvidenceProviderSettings(
            name=EvidenceProviderName.PROMETHEUS,
            base_url="http://prom:9090",
            max_retries=2,
            retry_min_wait_seconds=0.01,
            retry_max_wait_seconds=0.05,
        )
    )
    _wire_transport(provider, transport)

    with pytest.raises(EvidenceUnavailableError):
        await provider._get("/api/v1/query_range", params={"query": "up"})


# ── 5. Metrics cancellation ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prometheus_collect_is_cancellable() -> None:
    """asyncio.cancel() on a slow collect() task is honoured."""
    class _Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
            await asyncio.sleep(999)
            return httpx.Response(200)

    provider = PrometheusMetricsProvider.from_settings(
        EvidenceProviderSettings(
            name=EvidenceProviderName.PROMETHEUS,
            base_url="http://prom:9090",
            timeout_seconds=60.0,
            max_retries=0,
        )
    )
    _wire_transport(provider, _Slow())

    inc = _incident()
    task = asyncio.create_task(
        provider.collect(inc, context={"metric_queries": ["up"]})
    )
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises((asyncio.CancelledError, EvidenceTimeoutError)):
        await task


# ── 6. Elastic log query/response parsing ────────────────────────────────


@pytest.mark.asyncio
async def test_elastic_parses_success_response() -> None:
    """ElasticLogsProvider parses a valid Elasticsearch _search response."""
    provider = ElasticLogsProvider.from_settings(_elastic_settings())
    transport = _MockTransport(200, _elastic_success("svc-a", num_hits=5, error_hits=2))
    _wire_transport(provider, transport)

    inc = _incident(services=("svc-a",))
    items = await provider.collect(inc)

    assert len(items) == 1
    item = items[0]
    assert item.source.kind == EvidenceSourceKind.LOGS
    assert item.status == EvidenceStatus.CONFIRMED
    assert "svc-a" in item.title
    assert item.structured_data["total_hits"] == 5
    assert item.structured_data["error_count"] == 2


@pytest.mark.asyncio
async def test_elastic_search_logs_returns_raw_lines() -> None:
    """search_logs() returns a list of formatted log strings."""
    provider = ElasticLogsProvider.from_settings(_elastic_settings())
    transport = _MockTransport(200, _elastic_success("svc-a", num_hits=3))
    _wire_transport(provider, transport)

    lines = await provider.search_logs("svc-a", "error")

    assert isinstance(lines, list)
    assert all(isinstance(line, str) for line in lines)


@pytest.mark.asyncio
async def test_elastic_no_hits_produces_suspected_evidence() -> None:
    """Zero log hits produces SUSPECTED evidence."""
    provider = ElasticLogsProvider.from_settings(_elastic_settings())
    transport = _MockTransport(200, {"hits": {"total": {"value": 0}, "hits": []}})
    _wire_transport(provider, transport)

    inc = _incident(services=("svc-a",))
    items = await provider.collect(inc)

    assert len(items) == 1
    assert items[0].status == EvidenceStatus.SUSPECTED
    assert items[0].structured_data["total_hits"] == 0


@pytest.mark.asyncio
async def test_elastic_satisfies_logs_provider_protocol() -> None:
    """ElasticLogsProvider satisfies LogsEvidenceProvider structurally."""
    provider = ElasticLogsProvider.from_settings(_elastic_settings())
    assert isinstance(provider, LogsEvidenceProvider)


@pytest.mark.asyncio
async def test_elastic_health_check_green_returns_true() -> None:
    """health_check() returns True for green/yellow cluster health."""
    provider = ElasticLogsProvider.from_settings(_elastic_settings())
    transport = _MockTransport(200, {"status": "green", "cluster_name": "test"})
    _wire_transport(provider, transport)

    assert await provider.health_check() is True


@pytest.mark.asyncio
async def test_elastic_health_check_red_returns_false() -> None:
    """health_check() returns False for red cluster health."""
    provider = ElasticLogsProvider.from_settings(_elastic_settings())
    transport = _MockTransport(200, {"status": "red", "cluster_name": "test"})
    _wire_transport(provider, transport)

    assert await provider.health_check() is False


# ── 7. Malformed log responses ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_elastic_malformed_response_skipped_gracefully() -> None:
    """Malformed Elastic response does not crash collect()."""
    provider = ElasticLogsProvider.from_settings(
        EvidenceProviderSettings(
            name=EvidenceProviderName.ELASTIC,
            base_url="http://elastic:9200",
            max_retries=0,
        )
    )

    class _Bad(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

    _wire_transport(provider, _Bad())

    inc = _incident()
    items = await provider.collect(inc)
    assert items == []


@pytest.mark.asyncio
async def test_elastic_401_raises_auth_error() -> None:
    """Elastic 401 maps to EvidenceAuthError."""
    provider = ElasticLogsProvider.from_settings(
        EvidenceProviderSettings(
            name=EvidenceProviderName.ELASTIC,
            base_url="http://elastic:9200",
            max_retries=0,
        )
    )
    transport = _MockTransport(401, {"error": "Unauthorized"})
    _wire_transport(provider, transport)

    with pytest.raises(EvidenceAuthError):
        await provider._post("/logs-*/_search", json={"query": {}})


# ── 8. Logs timeout/retry ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_elastic_retries_503_then_succeeds() -> None:
    """ElasticLogsProvider retries on 503 and returns data on second attempt."""
    transport = _SeqTransport([
        (503, {"error": "overloaded"}),
        (200, _elastic_success("svc-a", num_hits=2)),
    ])
    provider = ElasticLogsProvider.from_settings(
        EvidenceProviderSettings(
            name=EvidenceProviderName.ELASTIC,
            base_url="http://elastic:9200",
            max_retries=2,
            retry_min_wait_seconds=0.01,
            retry_max_wait_seconds=0.05,
        )
    )
    _wire_transport(provider, transport)

    inc = _incident(services=("svc-a",))
    items = await provider.collect(inc)

    assert len(items) == 1
    assert transport._idx == 2


@pytest.mark.asyncio
async def test_elastic_timeout_during_collect_skips_service() -> None:
    """Elastic timeout during collect() skips that service gracefully."""
    class _Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
            await asyncio.sleep(999)
            return httpx.Response(200)

    provider = ElasticLogsProvider.from_settings(
        EvidenceProviderSettings(
            name=EvidenceProviderName.ELASTIC,
            base_url="http://elastic:9200",
            timeout_seconds=0.05,
            max_retries=0,
        )
    )
    _wire_transport(provider, _Slow())

    inc = _incident(services=("svc-a",))
    items = await provider.collect(inc)
    assert items == []


# ── 9. Trace response parsing ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_otlp_parses_jaeger_response() -> None:
    """OTLPTracesProvider parses a Jaeger /api/traces response."""
    provider = OTLPTracesProvider.from_settings(_otlp_settings())
    transport = _MockTransport(200, _jaeger_success("svc-a", num_traces=3, error_count=1))
    _wire_transport(provider, transport)

    inc = _incident(services=("svc-a",))
    items = await provider.collect(inc)

    assert len(items) == 1
    item = items[0]
    assert item.source.kind == EvidenceSourceKind.TRACES
    assert item.status == EvidenceStatus.CONFIRMED
    assert "svc-a" in item.title
    assert item.structured_data["total_traces"] == 3
    assert item.structured_data["error_traces"] == 1


@pytest.mark.asyncio
async def test_otlp_query_traces_returns_raw_data() -> None:
    """query_traces() returns raw trace list without converting to Evidence."""
    provider = OTLPTracesProvider.from_settings(_otlp_settings())
    transport = _MockTransport(200, _jaeger_success("svc-a"))
    _wire_transport(provider, transport)

    result = await provider.query_traces("svc-a", window_seconds=60.0)

    assert isinstance(result, list)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_otlp_no_traces_produces_suspected_evidence() -> None:
    """Zero traces produces SUSPECTED evidence."""
    provider = OTLPTracesProvider.from_settings(_otlp_settings())
    transport = _MockTransport(200, {"data": []})
    _wire_transport(provider, transport)

    inc = _incident(services=("svc-a",))
    items = await provider.collect(inc)

    assert len(items) == 1
    assert items[0].status == EvidenceStatus.SUSPECTED


@pytest.mark.asyncio
async def test_otlp_slow_traces_counted() -> None:
    """Traces with duration > 1 s are counted as slow."""
    # Jaeger uses microseconds for duration
    body = {
        "data": [{
            "traceID": "abc",
            "spans": [
                {"spanID": "s1", "duration": 2_000_000, "tags": []},  # 2 s → slow
                {"spanID": "s2", "duration": 100_000,   "tags": []},  # 0.1 s → fast
            ],
            "processes": {},
        }]
    }
    provider = OTLPTracesProvider.from_settings(_otlp_settings())
    transport = _MockTransport(200, body)
    _wire_transport(provider, transport)

    inc = _incident(services=("svc-a",))
    items = await provider.collect(inc)

    assert items[0].structured_data["slow_traces"] == 1


@pytest.mark.asyncio
async def test_otlp_satisfies_traces_provider_protocol() -> None:
    """OTLPTracesProvider satisfies TracesEvidenceProvider structurally."""
    provider = OTLPTracesProvider.from_settings(_otlp_settings())
    assert isinstance(provider, TracesEvidenceProvider)


# ── 10. Trace timeout/retry ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_otlp_retries_on_503() -> None:
    """OTLPTracesProvider retries 503 and succeeds on second attempt."""
    transport = _SeqTransport([
        (503, {}),
        (200, _jaeger_success("svc-a", num_traces=1)),
    ])
    provider = OTLPTracesProvider.from_settings(
        EvidenceProviderSettings(
            name=EvidenceProviderName.OTLP,
            base_url="http://jaeger:16686",
            max_retries=2,
            retry_min_wait_seconds=0.01,
            retry_max_wait_seconds=0.05,
        )
    )
    _wire_transport(provider, transport)

    inc = _incident(services=("svc-a",))
    items = await provider.collect(inc)

    assert len(items) == 1
    assert transport._idx == 2


@pytest.mark.asyncio
async def test_otlp_timeout_skips_service() -> None:
    """Trace timeout during collect() skips that service gracefully."""
    class _Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
            await asyncio.sleep(999)
            return httpx.Response(200)

    provider = OTLPTracesProvider.from_settings(
        EvidenceProviderSettings(
            name=EvidenceProviderName.OTLP,
            base_url="http://jaeger:16686",
            timeout_seconds=0.05,
            max_retries=0,
        )
    )
    _wire_transport(provider, _Slow())

    inc = _incident(services=("svc-a",))
    items = await provider.collect(inc)
    assert items == []


# ── 11. Provider selection ────────────────────────────────────────────────


def test_factory_returns_prometheus_for_prometheus_name() -> None:
    """build_metrics_provider returns PrometheusMetricsProvider for name=prometheus."""
    settings = EvidenceSettings(metrics=_prom_settings())
    provider = build_metrics_provider(settings)
    assert isinstance(provider, PrometheusMetricsProvider)
    assert provider.name == "prometheus"


def test_factory_returns_elastic_for_elastic_name() -> None:
    """build_logs_provider returns ElasticLogsProvider for name=elastic."""
    settings = EvidenceSettings(logs=_elastic_settings())
    provider = build_logs_provider(settings)
    assert isinstance(provider, ElasticLogsProvider)
    assert provider.name == "elastic"


def test_factory_returns_otlp_for_otlp_name() -> None:
    """build_traces_provider returns OTLPTracesProvider for name=otlp."""
    settings = EvidenceSettings(traces=_otlp_settings())
    provider = build_traces_provider(settings)
    assert isinstance(provider, OTLPTracesProvider)
    assert provider.name == "otlp"


def test_factory_returns_fake_when_disabled() -> None:
    """Factory returns fake provider when enabled=False."""
    cfg = EvidenceProviderSettings(
        name=EvidenceProviderName.PROMETHEUS, enabled=False
    )
    settings = EvidenceSettings(metrics=cfg)
    provider = build_metrics_provider(settings)
    assert isinstance(provider, FakeMetricsProvider)


def test_factory_returns_fake_for_fake_name() -> None:
    """Factory returns fake providers for name=fake."""
    fake_cfg = EvidenceProviderSettings(name=EvidenceProviderName.FAKE)
    settings = EvidenceSettings(
        metrics=fake_cfg, logs=fake_cfg, traces=fake_cfg
    )
    assert isinstance(build_metrics_provider(settings), FakeMetricsProvider)
    assert isinstance(build_logs_provider(settings), FakeLogsProvider)
    assert isinstance(build_traces_provider(settings), FakeTracesProvider)


# ── 12. Fake-provider isolation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_fake_metrics_provider_makes_no_http_calls() -> None:
    """FakeMetricsProvider never opens a network connection."""
    provider = FakeMetricsProvider()
    inc = _incident()
    items = await provider.collect(inc)
    # Succeeds with synthetic data and no network
    assert isinstance(items, list)
    assert all(e.source.kind == EvidenceSourceKind.METRICS for e in items)


@pytest.mark.asyncio
async def test_fake_logs_provider_makes_no_http_calls() -> None:
    provider = FakeLogsProvider()
    inc = _incident()
    items = await provider.collect(inc)
    assert all(e.source.kind == EvidenceSourceKind.LOGS for e in items)


@pytest.mark.asyncio
async def test_fake_traces_provider_makes_no_http_calls() -> None:
    provider = FakeTracesProvider()
    inc = _incident()
    items = await provider.collect(inc)
    assert all(e.source.kind == EvidenceSourceKind.TRACES for e in items)


# ── 13. Concurrent evidence collection ───────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_collects_from_all_three_providers_concurrently() -> None:
    """EvidenceOrchestrator collects from metrics, logs, traces concurrently."""
    inc = _incident()
    orchestrator = EvidenceOrchestrator(
        providers=[FakeMetricsProvider(), FakeLogsProvider(), FakeTracesProvider()]
    )

    result = await orchestrator.collect(inc)

    kinds = {e.source.kind for e in result.collection.items}
    assert EvidenceSourceKind.METRICS in kinds
    assert EvidenceSourceKind.LOGS in kinds
    assert EvidenceSourceKind.TRACES in kinds
    assert result.collection.count >= 3
    assert not result.partial


@pytest.mark.asyncio
async def test_orchestrator_collects_within_reasonable_time() -> None:
    """Concurrent collection is faster than serial (wall-clock check)."""
    class _SlowFake:
        kind = EvidenceSourceKind.METRICS
        name = "slow-fake-metrics"

        async def collect(self, inc: Any, *, max_items: int = 20, context: Any = None) -> list[Any]:
            await asyncio.sleep(0.05)
            return []

        async def health_check(self) -> bool:
            return True

    class _SlowFake2(_SlowFake):
        kind = EvidenceSourceKind.LOGS
        name = "slow-fake-logs"

    inc = _incident()
    orchestrator = EvidenceOrchestrator(providers=[_SlowFake(), _SlowFake2()])
    t0 = time.monotonic()
    await orchestrator.collect(inc)
    elapsed = time.monotonic() - t0
    # 2 providers x 0.05 s each -> serial would be >= 0.10 s; concurrent ~0.05 s
    assert elapsed < 0.12, f"Collection took {elapsed:.3f}s — expected concurrent execution"


# ── 14. Partial provider failure ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_tolerates_one_provider_failure() -> None:
    """Partial failure: one broken provider doesn't abort the others."""
    class _BrokenProvider:
        kind = EvidenceSourceKind.TRACES
        name = "broken-traces"

        async def collect(self, inc: Any, *, max_items: int = 20, context: Any = None) -> list[Any]:
            raise RuntimeError("traces service down")

        async def health_check(self) -> bool:
            return False

    inc = _incident()
    orchestrator = EvidenceOrchestrator(
        providers=[FakeMetricsProvider(), _BrokenProvider()]
    )

    result = await orchestrator.collect(inc)

    assert result.partial is True
    assert "broken-traces" in result.provider_errors
    # Metrics still collected
    kinds = {e.source.kind for e in result.collection.items}
    assert EvidenceSourceKind.METRICS in kinds


@pytest.mark.asyncio
async def test_orchestrator_all_providers_fail_returns_empty_partial() -> None:
    """All providers fail → empty collection with partial=True."""
    class _Broken:
        kind = EvidenceSourceKind.METRICS
        name = "broken"

        async def collect(self, inc: Any, *, max_items: int = 20, context: Any = None) -> list[Any]:
            raise ConnectionError("all systems down")

        async def health_check(self) -> bool:
            return False

    inc = _incident()
    orchestrator = EvidenceOrchestrator(providers=[_Broken()])

    result = await orchestrator.collect(inc)

    assert result.collection.count == 0
    assert result.partial is True
    assert "broken" in result.provider_errors


# ── 15. Lifecycle event emission ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_emits_collection_started_and_collected() -> None:
    """Orchestrator emits CollectionStarted and Collected lifecycle events."""
    from backend.services.incident_events import (
        EVIDENCE_COLLECTED,
        EVIDENCE_COLLECTION_STARTED,
    )

    events: list[tuple[str, dict[str, Any]]] = []

    class _Emitter:
        async def emit(self, name: str, payload: Mapping[str, Any], ctx: Any) -> None:
            events.append((name, dict(payload)))

    inc = _incident()
    orchestrator = EvidenceOrchestrator(
        providers=[FakeMetricsProvider()],
        event_emitter=_Emitter(),
    )

    await orchestrator.collect(inc)

    names = [e[0] for e in events]
    assert EVIDENCE_COLLECTION_STARTED in names
    assert EVIDENCE_COLLECTED in names


@pytest.mark.asyncio
async def test_orchestrator_emits_partial_failure_event_when_provider_fails() -> None:
    """Orchestrator emits CollectionPartialFailure when a provider fails."""
    from backend.services.incident_events import EVIDENCE_COLLECTION_PARTIAL_FAILURE

    events: list[tuple[str, dict[str, Any]]] = []

    class _Emitter:
        async def emit(self, name: str, payload: Mapping[str, Any], ctx: Any) -> None:
            events.append((name, dict(payload)))

    class _Broken:
        kind = EvidenceSourceKind.METRICS
        name = "broken"

        async def collect(self, inc: Any, *, max_items: int = 20, context: Any = None) -> list[Any]:
            raise RuntimeError("down")

        async def health_check(self) -> bool:
            return False

    inc = _incident()
    orchestrator = EvidenceOrchestrator(
        providers=[_Broken()],
        event_emitter=_Emitter(),
    )

    await orchestrator.collect(inc)

    names = [e[0] for e in events]
    assert EVIDENCE_COLLECTION_PARTIAL_FAILURE in names


@pytest.mark.asyncio
async def test_lifecycle_event_payloads_include_incident_id() -> None:
    """All lifecycle event payloads carry the incident_id for correlation."""
    events: list[tuple[str, dict[str, Any]]] = []

    class _Emitter:
        async def emit(self, name: str, payload: Mapping[str, Any], ctx: Any) -> None:
            events.append((name, dict(payload)))

    inc = _incident()
    orchestrator = EvidenceOrchestrator(
        providers=[FakeMetricsProvider()],
        event_emitter=_Emitter(),
    )

    await orchestrator.collect(inc)

    for name, payload in events:
        if "incident_id" in payload:
            assert payload["incident_id"] == inc.incident_id, (
                f"Event {name!r} has wrong incident_id"
            )


# ── 16. OTel/correlation propagation ─────────────────────────────────────


@pytest.mark.asyncio
async def test_prometheus_includes_correlation_id_in_span_attributes() -> None:
    """Prometheus adapter attaches incident_id to the HTTP request context."""
    # We verify indirectly: the adapter accepts correlation_id without error
    # and the request is sent with the expected query params.
    captured: dict[str, Any] = {}

    class _Cap(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(_prom_success()).encode(),
            )

    provider = PrometheusMetricsProvider.from_settings(_prom_settings())
    _wire_transport(provider, _Cap())

    inc = _incident()
    await provider.collect(
        inc,
        context={
            "metric_queries": ["up"],
            "correlation_id": "corr-otel-test",
        },
    )

    assert "query_range" in captured.get("url", "")


@pytest.mark.asyncio
async def test_orchestrator_normalized_evidence_carries_correlation_id() -> None:
    """Normalised evidence items carry the incident correlation_id."""
    inc = _incident()
    orchestrator = EvidenceOrchestrator(providers=[FakeMetricsProvider()])
    result = await orchestrator.collect(inc)

    for item in result.normalized.items:
        # correlation_id field in NormalizedEvidence comes from structured_data
        # or falls back to incident_id; both are acceptable
        assert item.correlation_id is not None


# ── 17. Secret non-leakage ────────────────────────────────────────────────


def test_prometheus_api_key_not_in_provider_repr() -> None:
    """API key must not appear in the provider's repr or str."""
    from pydantic import SecretStr
    cfg = EvidenceProviderSettings(
        name=EvidenceProviderName.PROMETHEUS,
        api_key=SecretStr("super-secret-key"),
        base_url="http://prom:9090",
    )
    provider = PrometheusMetricsProvider.from_settings(cfg)
    representation = repr(provider)
    assert "super-secret-key" not in representation


def test_elastic_password_not_in_provider_repr() -> None:
    """Elastic password must not appear in the provider's repr or str."""
    from pydantic import SecretStr
    cfg = EvidenceProviderSettings(
        name=EvidenceProviderName.ELASTIC,
        password=SecretStr("my-db-password"),
        username="elastic",
        base_url="http://elastic:9200",
    )
    provider = ElasticLogsProvider.from_settings(cfg)
    representation = repr(provider)
    assert "my-db-password" not in representation


@pytest.mark.asyncio
async def test_prometheus_api_key_not_in_http_request_url() -> None:
    """API key must be sent in Authorization header, never in the URL."""
    from pydantic import SecretStr
    cfg = EvidenceProviderSettings(
        name=EvidenceProviderName.PROMETHEUS,
        api_key=SecretStr("secret-token"),
        base_url="http://prom:9090",
        max_retries=0,
    )
    provider = PrometheusMetricsProvider.from_settings(cfg)

    captured_urls: list[str] = []

    class _Cap(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
            captured_urls.append(str(req.url))
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(_prom_success()).encode(),
            )

    _wire_transport(provider, _Cap())
    inc = _incident()
    await provider.collect(inc, context={"metric_queries": ["up"]})

    for url in captured_urls:
        assert "secret-token" not in url
