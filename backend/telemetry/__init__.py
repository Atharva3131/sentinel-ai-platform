"""OpenTelemetry setup and lifecycle."""

from backend.telemetry.configuration import TelemetryHandle, configure_telemetry

__all__ = ["TelemetryHandle", "configure_telemetry"]
