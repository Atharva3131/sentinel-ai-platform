"""Vendor-neutral OpenTelemetry configuration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.configuration.settings import AppSettings


@dataclass(slots=True)
class TelemetryHandle:
    """Own explicit instrumentation and provider shutdown."""

    provider: TracerProvider | None = None
    sqlalchemy_instrumented: bool = False

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
        if self.provider is not None:
            await asyncio.to_thread(self.provider.shutdown)


def configure_telemetry(settings: AppSettings, app: FastAPI) -> TelemetryHandle:
    """Configure tracing and instrument the FastAPI application."""
    if not settings.opentelemetry.enabled:
        return TelemetryHandle()

    resource = Resource.create(
        {
            SERVICE_NAME: settings.app_name,
            SERVICE_VERSION: settings.app_version,
            "deployment.environment.name": settings.environment,
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

    excluded_urls = "/health.*" if settings.opentelemetry.exclude_health_endpoints else None
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=excluded_urls,
    )
    return TelemetryHandle(provider=provider)


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
