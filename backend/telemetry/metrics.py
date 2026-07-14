"""OpenTelemetry metric instruments used across the application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opentelemetry.sdk.metrics import MeterProvider


@dataclass(slots=True)
class TelemetryMetrics:
    """Typed metric instruments for requests and dependency health."""

    meter_provider: MeterProvider | None
    _http_requests_total: Any = None
    _http_request_duration_ms: Any = None
    _health_checks_total: Any = None
    _health_check_duration_ms: Any = None

    def __post_init__(self) -> None:
        if self.meter_provider is None:
            return
        meter = self.meter_provider.get_meter("sentinel-ai-platform")
        self._http_requests_total = meter.create_counter(
            name="http_server_requests_total",
            description="Total HTTP requests observed by the API server.",
        )
        self._http_request_duration_ms = meter.create_histogram(
            name="http_server_request_duration_ms",
            unit="ms",
            description="HTTP request latency in milliseconds.",
        )
        self._health_checks_total = meter.create_counter(
            name="health_check_total",
            description="Total health check observations.",
        )
        self._health_check_duration_ms = meter.create_histogram(
            name="health_check_duration_ms",
            unit="ms",
            description="Dependency health-check latency in milliseconds.",
        )

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
        """Record an HTTP request metric observation."""
        if self._http_requests_total is None or self._http_request_duration_ms is None:
            return
        attributes = {
            "http.method": method or "unknown",
            "http.route": path or "unknown",
            "http.status_code": status_code,
        }
        if correlation_id is not None:
            attributes["correlation.id"] = correlation_id
        if workflow_id is not None:
            attributes["workflow.id"] = workflow_id
        if execution_id is not None:
            attributes["execution.id"] = execution_id
        self._http_requests_total.add(1, attributes=attributes)
        self._http_request_duration_ms.record(duration_ms, attributes=attributes)

    def record_health_check(
        self,
        *,
        name: str,
        status: str,
        duration_ms: float,
    ) -> None:
        """Record a dependency health metric observation."""
        if self._health_checks_total is None or self._health_check_duration_ms is None:
            return
        attributes = {
            "dependency.name": name,
            "dependency.status": status,
        }
        self._health_checks_total.add(1, attributes=attributes)
        self._health_check_duration_ms.record(duration_ms, attributes=attributes)

