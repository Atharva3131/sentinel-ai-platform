"""Incident domain models — the entry point for every autonomous response."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class IncidentSeverity(StrEnum):
    """Impact level of an incident."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IncidentStatus(StrEnum):
    """Lifecycle state of an incident."""

    OPEN = "open"
    INVESTIGATING = "investigating"
    REMEDIATING = "remediating"
    VALIDATING = "validating"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


class SignalType(StrEnum):
    """Category of the incoming operational signal."""

    ALERT = "alert"
    METRIC_THRESHOLD = "metric_threshold"
    LOG_PATTERN = "log_pattern"
    TRACE_ANOMALY = "trace_anomaly"
    DEPLOYMENT_EVENT = "deployment_event"
    HEALTH_CHECK_FAILURE = "health_check_failure"
    USER_REPORT = "user_report"
    SYNTHETIC_TEST = "synthetic_test"
    OTHER = "other"


class SignalSource(StrEnum):
    """System that produced the signal."""

    PROMETHEUS = "prometheus"
    DATADOG = "datadog"
    CLOUDWATCH = "cloudwatch"
    GRAFANA = "grafana"
    PAGERDUTY = "pagerduty"
    OPSGENIE = "opsgenie"
    SPLUNK = "splunk"
    ELASTIC = "elastic"
    GITHUB_ACTIONS = "github_actions"
    MANUAL = "manual"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class IncidentSignal:
    """A single operational signal that contributed to incident detection.

    Signals are the raw inputs: an alert firing, a metric crossing a threshold,
    a log pattern match, a deployment event.  Multiple signals may correlate
    into a single incident.
    """

    signal_id: str
    signal_type: SignalType
    source: SignalSource
    title: str
    description: str
    severity: IncidentSeverity
    received_at: datetime
    service: str | None = None
    environment: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Incident:
    """The canonical incident record for the autonomous response lifecycle.

    An incident aggregates one or more correlated signals into a single
    actionable unit.  The platform operates on ``Incident`` objects — it is
    incident-type agnostic and does not branch on specific failure modes.

    ``affected_services`` is an ordered tuple of service identifiers.
    ``signals`` captures the raw inputs that triggered detection.
    ``symptoms`` are human-readable symptom descriptions derived from signals.
    ``correlation_id`` links all events across the full workflow.
    """

    incident_id: str
    title: str
    severity: IncidentSeverity
    status: IncidentStatus
    affected_services: tuple[str, ...]
    description: str
    detected_at: datetime
    signals: tuple[IncidentSignal, ...] = ()
    symptoms: tuple[str, ...] = ()
    tenant_id: str | None = None
    environment: str | None = None
    correlation_id: str | None = None
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def with_status(self, status: IncidentStatus) -> Incident:
        """Return a copy with an updated status (value objects are immutable)."""
        from dataclasses import replace
        return replace(self, status=status)
