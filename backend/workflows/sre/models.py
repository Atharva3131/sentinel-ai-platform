"""SRE workflow value objects — incident, plan, analysis, confidence, recovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from backend.evaluation.models import EvaluationReport


class IncidentSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IncidentStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"


class ConfidenceLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ---------------------------------------------------------------------------
# Incident
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IncidentContext:
    """Immutable representation of an incoming incident.

    ``affected_services`` and ``symptoms`` are ordered tuples; callers should
    sort them if order-independence is required.
    """

    incident_id: str
    title: str
    severity: IncidentSeverity
    affected_services: tuple[str, ...]
    description: str
    symptoms: tuple[str, ...] = ()
    status: IncidentStatus = IncidentStatus.OPEN
    tenant_id: str | None = None
    source: str | None = None
    correlation_id: str | None = None
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One step in the execution plan.

    ``depends_on`` lists step_ids that must complete before this one starts.
    ``agent_name`` identifies the agent responsible for executing the step.
    """

    step_id: str
    name: str
    agent_name: str
    depends_on: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Ordered execution plan produced by the planning phase.

    ``stages`` groups step_ids into topologically ordered execution batches;
    steps within the same stage may run concurrently.
    """

    plan_id: str
    incident_id: str
    steps: tuple[PlanStep, ...]
    stages: tuple[tuple[str, ...], ...]
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Findings produced by the analysis phase.

    ``root_cause_hypothesis`` is a human-readable explanation synthesised from
    retrieved evidence and health observations; None when evidence is insufficient.
    ``graph_depth_reached`` is the Neo4j traversal depth used for context retrieval.
    """

    incident_id: str
    completed_steps: tuple[str, ...]
    failed_steps: tuple[str, ...]
    findings: tuple[str, ...]
    affected_scope: tuple[str, ...]
    retrieved_chunk_count: int
    root_cause_hypothesis: str | None = None
    graph_depth_reached: int | None = None
    health_status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConfidenceAssessment:
    """Output of the confidence evaluation phase.

    ``requires_approval`` is True when ``score`` falls below the configured
    approval threshold and human confirmation must be obtained before recovery.
    """

    score: float
    level: ConfidenceLevel
    requires_approval: bool
    reasoning: str
    evaluation_report: EvaluationReport | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Request emitted when human approval is required before recovery."""

    request_id: str
    incident_id: str
    plan: ExecutionPlan
    analysis: AnalysisResult
    confidence: ConfidenceAssessment
    requested_at: datetime
    timeout_seconds: float = 300.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """Recorded decision from the approval gate."""

    request_id: str
    decision: ApprovalDecision
    decided_at: datetime
    approver_id: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    """One atomic remediation step to execute against a service.

    ``action_type`` is an allowlisted verb (e.g. 'restart_service',
    'rollback_deployment', 'scale_up', 'clear_cache').
    """

    action_id: str
    name: str
    target_service: str
    action_type: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Aggregated outcome of executing all recovery actions."""

    incident_id: str
    actions_attempted: int
    actions_succeeded: int
    actions_failed: int
    recovered: bool
    duration_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """Immutable audit-log record for one significant workflow event.

    Every phase transition, approval, and recovery action produces an entry.
    ``details`` is unstructured and may include phase-specific payloads.
    """

    entry_id: str
    incident_id: str
    phase: str
    event: str
    actor: str
    timestamp: datetime
    workflow_id: str | None = None
    execution_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Final result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SREWorkflowResult:
    """Complete output of one SRE workflow execution.

    ``status`` is one of: 'completed', 'failed', 'rejected', 'escalated',
    'cancelled', 'timed_out'.
    """

    incident_id: str
    status: str
    audit_entries: tuple[AuditEntry, ...]
    duration_ms: float
    workflow_id: str | None = None
    execution_id: str | None = None
    plan: ExecutionPlan | None = None
    analysis: AnalysisResult | None = None
    confidence: ConfidenceAssessment | None = None
    approval: ApprovalOutcome | None = None
    recovery: RecoveryResult | None = None
    evaluation_report: EvaluationReport | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
