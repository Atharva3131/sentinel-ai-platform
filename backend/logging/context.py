"""Structured logging context helpers."""

from __future__ import annotations

from opentelemetry import trace
from structlog.types import EventDict, WrappedLogger


def add_opentelemetry_context(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Attach trace context from the active OpenTelemetry span."""
    span = trace.get_current_span()
    span_context = span.get_span_context() if span is not None else None
    if span_context is None or not span_context.is_valid:
        return event_dict

    event_dict["trace_id"] = f"{span_context.trace_id:032x}"
    event_dict["span_id"] = f"{span_context.span_id:016x}"
    event_dict["trace_sampled"] = span_context.trace_flags.sampled
    return event_dict
