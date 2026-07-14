"""OpenTelemetry infrastructure tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any, ClassVar, cast

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry import baggage
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from structlog.contextvars import clear_contextvars

from backend.api.routers.metrics import router as metrics_router
from backend.application.health import HealthService
from backend.configuration.settings import (
    AppSettings,
    LoggingSettings,
    OpenTelemetrySettings,
)
from backend.middleware import RequestContextMiddleware
from backend.telemetry import bind_request_context, detach_request_context
from backend.telemetry.configuration import TelemetryHandle


@pytest.fixture(autouse=True)
def reset_contextvars() -> Generator[None, None, None]:
    """Prevent cross-test leakage of logging and baggage context."""
    clear_contextvars()
    yield
    clear_contextvars()


class FakeCounter:
    """Metric counter double."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, dict[str, Any] | None]] = []

    def add(self, value: float, attributes: dict[str, Any] | None = None) -> None:
        self.calls.append((value, attributes))


class FakeHistogram:
    """Metric histogram double."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, dict[str, Any] | None]] = []

    def record(self, value: float, attributes: dict[str, Any] | None = None) -> None:
        self.calls.append((value, attributes))


class FakeMeter:
    """Meter double used to inspect emitted measurements."""

    def __init__(self) -> None:
        self.counters: dict[str, FakeCounter] = {}
        self.histograms: dict[str, FakeHistogram] = {}

    def create_counter(self, name: str, **_: Any) -> FakeCounter:
        counter = FakeCounter()
        self.counters[name] = counter
        return counter

    def create_histogram(self, name: str, **_: Any) -> FakeHistogram:
        histogram = FakeHistogram()
        self.histograms[name] = histogram
        return histogram


class FakeMeterProvider:
    """Meter provider double."""

    def __init__(self) -> None:
        self.meter = FakeMeter()
        self.shutdown_called = False

    def get_meter(self, name: str) -> FakeMeter:
        self.meter_name = name
        return self.meter

    def shutdown(self) -> None:
        self.shutdown_called = True


class FakeTracerProvider:
    """Tracer provider double."""

    def __init__(self) -> None:
        self.span_processors: list[Any] = []
        self.shutdown_called = False

    def add_span_processor(self, processor: Any) -> None:
        self.span_processors.append(processor)

    def shutdown(self) -> None:
        self.shutdown_called = True


class FakePrometheusMetricReader:
    """Prometheus reader stub."""

    def __init__(self, *_: Any, **__: Any) -> None:
        self.initialized = True


class FakeSpanProcessor:
    """Span processor stub."""

    def __init__(self, exporter: Any) -> None:
        self.exporter = exporter


class FakeFastAPIInstrumentor:
    """FastAPI instrumentor stub."""

    instrument_calls: ClassVar[list[dict[str, Any]]] = []
    uninstrument_calls: ClassVar[list[Any]] = []

    @classmethod
    def instrument_app(cls, app: FastAPI, **kwargs: Any) -> None:
        cls.instrument_calls.append({"app": app, "kwargs": kwargs})

    @classmethod
    def uninstrument_app(cls, app: FastAPI) -> None:
        cls.uninstrument_calls.append(app)


class FakeInstrumentor:
    """Generic instrumentor stub."""

    instrument_calls: ClassVar[list[dict[str, Any]]] = []
    uninstrument_calls: ClassVar[int] = 0

    def instrument(self, **kwargs: Any) -> None:
        type(self).instrument_calls.append(kwargs)

    def uninstrument(self) -> None:
        type(self).uninstrument_calls += 1


class FakeTelemetryHandle:
    """Minimal telemetry sink used to verify middleware and health recording."""

    def __init__(self) -> None:
        self.http_requests: list[dict[str, Any]] = []
        self.health_checks: list[dict[str, Any]] = []

    def record_http_request(self, **kwargs: Any) -> None:
        self.http_requests.append(kwargs)

    def record_health_check(self, **kwargs: Any) -> None:
        self.health_checks.append(kwargs)


class DummyCheck:
    """Health check double."""

    name = "dummy"

    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls = 0

    async def check(self) -> None:
        self.calls += 1
        if self.should_fail:
            raise RuntimeError("boom")


def _settings() -> AppSettings:
    return AppSettings(
        environment="development",
        logging=LoggingSettings(level="INFO", json_output=True),
        opentelemetry=OpenTelemetrySettings(
            enabled=True,
            otlp_http_endpoint="http://collector:4318/v1/traces",
            trace_sample_ratio=1.0,
            exclude_health_endpoints=True,
        ),
        neo4j={"enabled": False},
        cosmos={"enabled": False},
        blob={"enabled": False},
    )


@pytest.mark.asyncio
async def test_configure_telemetry_wires_tracing_metrics_and_instrumentors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telemetry bootstrap should create providers and instrument key libraries."""
    import backend.telemetry.configuration as cfg

    tracer_provider = FakeTracerProvider()
    meter_provider = FakeMeterProvider()

    monkeypatch.setattr(cfg, "PrometheusMetricReader", FakePrometheusMetricReader)
    monkeypatch.setattr(cfg, "OTLPSpanExporter", lambda **kwargs: kwargs)
    monkeypatch.setattr(cfg, "BatchSpanProcessor", FakeSpanProcessor)
    monkeypatch.setattr(cfg, "FastAPIInstrumentor", FakeFastAPIInstrumentor)
    monkeypatch.setattr(cfg, "HTTPXClientInstrumentor", FakeInstrumentor)
    monkeypatch.setattr(cfg, "RedisInstrumentor", FakeInstrumentor)
    monkeypatch.setattr(cfg, "SQLAlchemyInstrumentor", FakeInstrumentor)
    monkeypatch.setattr(cfg, "TracerProvider", lambda **_: tracer_provider)
    monkeypatch.setattr(cfg, "MeterProvider", lambda **_: meter_provider)
    monkeypatch.setattr(otel_trace, "set_tracer_provider", lambda provider: None)
    monkeypatch.setattr(otel_metrics, "set_meter_provider", lambda provider: None)

    app = FastAPI()
    handle = cfg.configure_telemetry(_settings(), app)

    assert app.state.telemetry is handle
    assert handle.provider is not None
    assert handle.meter_provider is not None
    assert handle.metrics is not None
    assert handle.fastapi_instrumented is True
    assert handle.httpx_instrumented is True
    assert handle.redis_instrumented is True

    handle.record_http_request(
        method="GET",
        path="/health/live",
        status_code=200,
        duration_ms=12.5,
        correlation_id="corr-1",
        workflow_id="workflow-1",
        execution_id="execution-1",
    )
    handle.record_health_check(name="redis", status="up", duration_ms=1.2)

    assert meter_provider.meter.counters["http_server_requests_total"].calls == [
        (
            1,
            {
                "http.method": "GET",
                "http.route": "/health/live",
                "http.status_code": 200,
                "correlation.id": "corr-1",
                "workflow.id": "workflow-1",
                "execution.id": "execution-1",
            },
        )
    ]
    assert meter_provider.meter.histograms["health_check_duration_ms"].calls == [
        (
            1.2,
            {
                "dependency.name": "redis",
                "dependency.status": "up",
            },
        )
    ]

    await handle.shutdown()
    assert tracer_provider.shutdown_called is True
    assert meter_provider.shutdown_called is True


def test_bind_request_context_attaches_baggage_and_span_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request context binding should populate baggage and span attributes."""
    class _SpanContext:
        is_valid = True

    class _Span:
        def __init__(self) -> None:
            self.attributes: dict[str, Any] = {}

        def get_span_context(self) -> _SpanContext:
            return _SpanContext()

        def set_attribute(self, key: str, value: Any) -> None:
            self.attributes[key] = value

    span = _Span()
    monkeypatch.setattr("backend.telemetry.context.trace.get_current_span", lambda: span)

    token = bind_request_context(
        correlation_id="corr-2",
        request_id="req-2",
        workflow_id="wf-2",
        execution_id="ex-2",
    )

    assert baggage.get_baggage("correlation_id") == "corr-2"
    assert baggage.get_baggage("request_id") == "req-2"
    assert baggage.get_baggage("workflow_id") == "wf-2"
    assert baggage.get_baggage("execution_id") == "ex-2"
    assert span.attributes["correlation.id"] == "corr-2"
    assert span.attributes["request.id"] == "req-2"
    assert span.attributes["workflow.id"] == "wf-2"
    assert span.attributes["execution.id"] == "ex-2"

    detach_request_context(token)


@pytest.mark.asyncio
async def test_request_context_middleware_records_http_metrics() -> None:
    """The request middleware should emit HTTP metrics and preserve request IDs."""
    app = FastAPI()
    telemetry = FakeTelemetryHandle()
    app.state.telemetry = telemetry
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"status": "ok"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/ping",
            headers={
                "X-Correlation-ID": "corr-http",
                "X-Workflow-ID": "workflow-http",
                "X-Execution-ID": "execution-http",
            },
        )

    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "corr-http"
    assert response.headers["X-Workflow-ID"] == "workflow-http"
    assert response.headers["X-Execution-ID"] == "execution-http"
    assert telemetry.http_requests[0]["correlation_id"] == "corr-http"
    assert telemetry.http_requests[0]["workflow_id"] == "workflow-http"
    assert telemetry.http_requests[0]["execution_id"] == "execution-http"


@pytest.mark.asyncio
async def test_health_service_records_latency_metrics() -> None:
    """Readiness checks should emit health-check metrics."""
    telemetry = FakeTelemetryHandle()
    service = HealthService(
        [DummyCheck()],
        timeout_seconds=1.0,
        telemetry=cast(TelemetryHandle, telemetry),
    )

    report = await service.readiness()

    assert report.ready is True
    assert telemetry.health_checks[0]["name"] == "dummy"
    assert telemetry.health_checks[0]["status"] == "up"


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_prometheus_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Prometheus endpoint should expose the collector payload."""
    monkeypatch.setattr("backend.api.routers.metrics.generate_latest", lambda: b"sentinel 1\n")

    app = FastAPI()
    app.include_router(metrics_router)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.text == "sentinel 1\n"
    assert response.headers["content-type"].startswith("text/plain")
