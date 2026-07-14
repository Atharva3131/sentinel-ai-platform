"""OpenTelemetry request context helpers."""

from __future__ import annotations

from typing import Any

from opentelemetry import baggage, trace
from opentelemetry.context import attach, detach


def bind_request_context(
    *,
    correlation_id: str,
    request_id: str,
    workflow_id: str | None = None,
    execution_id: str | None = None,
) -> Any:
    """Attach request identifiers to OpenTelemetry baggage and the active span."""
    telemetry_context = baggage.set_baggage("correlation_id", correlation_id)
    telemetry_context = baggage.set_baggage("request_id", request_id, context=telemetry_context)
    if workflow_id is not None:
        telemetry_context = baggage.set_baggage(
            "workflow_id",
            workflow_id,
            context=telemetry_context,
        )
    if execution_id is not None:
        telemetry_context = baggage.set_baggage(
            "execution_id",
            execution_id,
            context=telemetry_context,
        )

    token = attach(telemetry_context)
    span = trace.get_current_span()
    span_context = span.get_span_context() if span is not None else None
    if span_context is not None and span_context.is_valid:
        span.set_attribute("correlation.id", correlation_id)
        span.set_attribute("request.id", request_id)
        if workflow_id is not None:
            span.set_attribute("workflow.id", workflow_id)
        if execution_id is not None:
            span.set_attribute("execution.id", execution_id)
    return token


def detach_request_context(token: Any) -> None:
    """Detach request telemetry baggage from the active context."""
    detach(token)
