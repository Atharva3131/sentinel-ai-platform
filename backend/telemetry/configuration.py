"""Vendor-neutral OpenTelemetry configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.configuration.settings import AppSettings
from backend.telemetry.metrics import TelemetryMetrics


@dataclass(slots=True)
class TelemetryHandle:
    """Own explicit instrumentation and provider shutdown."""

    provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    metrics: TelemetryMetrics | None = None
    app: FastAPI | None = None
    sqlalchemy_instrumented: bool = False
    redis_instrumented: bool = False
    httpx_instrumented: bool = False
    fastapi_instrumented: bool = False
    _instrumentors: dict[str, Any] = field(default_factory=dict)

    def record_http_request(
        self,
        *,
        method: str | None,
        path: str | None,
        status_code: int,
        duration_ms: float,
        correlation_id: str | None = None,
        workflow_id: str | None = None,
        execution_id: str | None = None,
    ) -> None:
        """Record an HTTP request observation when metrics are enabled."""
        if self.metrics is None:
            return
        self.metrics.record_http_request(
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=duration_ms,
            correlation_id=correlation_id,
            workflow_id=workflow_id,
            execution_id=execution_id,
        )

    def record_health_check(
        self,
        *,
        name: str,
        status: str,
        duration_ms: float,
    ) -> None:
        """Record a dependency health observation when metrics are enabled."""
        if self.metrics is None:
            return
        self.metrics.record_health_check(
            name=name,
            status=status,
            duration_ms=duration_ms,
        )

    def instrument_sqlalchemy(self, engine: AsyncEngine) -> None:
        """Instrument the SQLAlchemy engine after it has been constructed."""
        if self.provider is None or self.sqlalchemy_instrumented:
            return
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
        self.sqlalchemy_instrumented = True

    async def shutdown(self) -> None:
        """Flush telemetry without blocking the event loop."""
        if self.sqlalchemy_instrumented:
            SQLAlchemyInstrumentor().uninstrument()
            self.sqlalchemy_instrumented = False
        if self.redis_instrumented:
            RedisInstrumentor().uninstrument()
            self.redis_instrumented = False
        if self.httpx_instrumented:
            HTTPXClientInstrumentor().uninstrument()
            self.httpx_instrumented = False
        if self.fastapi_instrumented and self.app is not None:
            uninstrument_app = getattr(FastAPIInstrumentor(), "uninstrument_app", None)
            if uninstrument_app is not None:
                try:
                    uninstrument_app(self.app)
                except Exception:
                    pass
            self.fastapi_instrumented = False
        if self.meter_provider is not None:
            self.meter_provider.shutdown()
        if self.provider is not None:
            self.provider.shutdown()


def configure_telemetry(settings: AppSettings, app: FastAPI) -> TelemetryHandle:
    """Configure tracing and metrics and instrument the FastAPI application."""
    handle = TelemetryHandle(app=app)
    app.state.telemetry = handle

    if not settings.opentelemetry.enabled:
        return handle

    resource = Resource.create(
        {
            SERVICE_NAME: settings.app_name,
            SERVICE_VERSION: settings.app_version,
            "deployment.environment.name": settings.environment.value,
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(settings.opentelemetry.trace_sample_ratio)),
    )
    if settings.opentelemetry.otlp_http_endpoint:
        exporter = OTLPSpanExporter(
            endpoint=settings.opentelemetry.otlp_http_endpoint,
            headers=_parse_otlp_headers(settings.opentelemetry.otlp_headers),
            timeout=settings.opentelemetry.export_timeout_seconds,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    metric_reader = PrometheusMetricReader()
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    handle.provider = provider
    handle.meter_provider = meter_provider
    handle.metrics = TelemetryMetrics(meter_provider)

    excluded_urls = (
        "/health.*|/metrics"
        if settings.opentelemetry.exclude_health_endpoints
        else "/metrics"
    )
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=excluded_urls,
    )
    handle.fastapi_instrumented = True

    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    handle.httpx_instrumented = True

    RedisInstrumentor().instrument(tracer_provider=provider)
    handle.redis_instrumented = True

    return handle


def _parse_otlp_headers(headers: SecretStr | None) -> dict[str, str] | None:
    """Convert `key=value,key=value` header strings into exporter headers."""
    if not headers:
        return None

    parsed_headers: dict[str, str] = {}
    for item in headers.get_secret_value().split(","):
        key, separator, value = item.partition("=")
        if not separator:
            continue
        parsed_headers[key.strip()] = value.strip()
    return parsed_headers or None
