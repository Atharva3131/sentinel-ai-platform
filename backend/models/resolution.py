"""Incident resolution domain model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from backend.models.hypothesis import RootCauseAnalysis
from backend.models.remediation import RemediationResult
from backend.models.validation import VerificationResult


class ResolutionStatus(StrEnum):
    """Final disposition of an incident."""

    RESOLVED = "resolved"             # fully resolved, verified
    MITIGATED = "mitigated"           # symptoms suppressed, root cause not fixed
    ESCALATED = "escalated"           # beyond autonomous capability
    FAILED = "failed"                 # autonomous remediation failed
    CANCELLED = "cancelled"           # operator cancelled
    INCONCLUSIVE = "inconclusive"     # resolution uncertain


@dataclass(frozen=True, slots=True)
class IncidentResolution:
    """Complete record of how an incident was resolved.

    This is the terminal output of the full autonomous incident-response
    lifecycle: detection → evidence → RCA → remediation → validation → resolution.

    Every field is optional except the incident identity and status, because
    the lifecycle may terminate early (e.g. escalated before RCA completes).
    """

    resolution_id: str
    incident_id: str
    status: ResolutionStatus
    resolved_at: datetime
    duration_ms: float
    workflow_id: str | None = None
    execution_id: str | None = None
    rca: RootCauseAnalysis | None = None
    remediation_result: RemediationResult | None = None
    verification_result: VerificationResult | None = None
    summary: str = ""
    lessons_learned: tuple[str, ...] = ()
    audit_entry_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_successful(self) -> bool:
        return self.status in (ResolutionStatus.RESOLVED, ResolutionStatus.MITIGATED)

    @property
    def required_escalation(self) -> bool:
        return self.status == ResolutionStatus.ESCALATED
