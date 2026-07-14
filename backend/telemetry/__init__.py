"""OpenTelemetry setup and lifecycle."""

from backend.telemetry.configuration import TelemetryHandle, configure_telemetry
from backend.telemetry.context import bind_request_context, detach_request_context
from backend.telemetry.metrics import TelemetryMetrics

__all__ = [
    "TelemetryHandle",
    "TelemetryMetrics",
    "bind_request_context",
    "configure_telemetry",
    "detach_request_context",
]
