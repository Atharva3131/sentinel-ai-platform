"""AI SRE Workflow — public API surface."""

from __future__ import annotations

from backend.workflows.sre.analyzer import SREAnalyzer
from backend.workflows.sre.auditor import WorkflowAuditor
from backend.workflows.sre.confidence import ConfidenceEvaluator
from backend.workflows.sre.context_builder import IncidentContextBuilder
from backend.workflows.sre.definition import register_sre_workflow, sre_workflow_definition
from backend.workflows.sre.events import (
    SRE_ANALYSIS_COMPLETED,
    SRE_ANALYSIS_STARTED,
    SRE_APPROVAL_RECEIVED,
    SRE_APPROVAL_REQUESTED,
    SRE_CONFIDENCE_EVALUATED,
    SRE_CONFIDENCE_EVALUATION_STARTED,
    SRE_CONTEXT_RETRIEVAL_STARTED,
    SRE_CONTEXT_RETRIEVED,
    SRE_PLAN_BUILD_STARTED,
    SRE_PLAN_BUILT,
    SRE_RECOVERY_COMPLETED,
    SRE_RECOVERY_STARTED,
    SRE_WORKFLOW_CANCELLED,
    SRE_WORKFLOW_COMPLETED,
    SRE_WORKFLOW_FAILED,
    SRE_WORKFLOW_STARTED,
)
from backend.workflows.sre.exceptions import (
    ApprovalTimeoutError,
    AuditError,
    IncidentContextError,
    SREPhaseError,
    SREWorkflowError,
)
from backend.workflows.sre.models import (
    AnalysisResult,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    AuditEntry,
    ConfidenceAssessment,
    ConfidenceLevel,
    ExecutionPlan,
    IncidentContext,
    IncidentSeverity,
    IncidentStatus,
    PlanStep,
    RecoveryAction,
    RecoveryResult,
    SREWorkflowResult,
)
from backend.workflows.sre.orchestrator import SREWorkflowOrchestrator
from backend.workflows.sre.plan_builder import SREPlanBuilder
from backend.workflows.sre.ports import (
    AgentRunnerPort,
    ApprovalGateway,
    AuditRepository,
    KnowledgeRetrieverPort,
    RecoveryExecutor,
)
from backend.workflows.sre.recovery import RecoveryCoordinator
from backend.workflows.sre.retrieval_evaluator_helpers import _make_evaluation_registry
from backend.workflows.sre.runtime import SREWorkflowRuntime

__all__ = [
    "SRE_ANALYSIS_COMPLETED",
    "SRE_ANALYSIS_STARTED",
    "SRE_APPROVAL_RECEIVED",
    "SRE_APPROVAL_REQUESTED",
    "SRE_CONFIDENCE_EVALUATED",
    "SRE_CONFIDENCE_EVALUATION_STARTED",
    "SRE_CONTEXT_RETRIEVAL_STARTED",
    "SRE_CONTEXT_RETRIEVED",
    "SRE_PLAN_BUILD_STARTED",
    "SRE_PLAN_BUILT",
    "SRE_RECOVERY_COMPLETED",
    "SRE_RECOVERY_STARTED",
    "SRE_WORKFLOW_CANCELLED",
    "SRE_WORKFLOW_COMPLETED",
    "SRE_WORKFLOW_FAILED",
    "SRE_WORKFLOW_STARTED",
    "AgentRunnerPort",
    "AnalysisResult",
    "ApprovalDecision",
    "ApprovalGateway",
    "ApprovalOutcome",
    "ApprovalRequest",
    "ApprovalTimeoutError",
    "AuditEntry",
    "AuditError",
    "AuditRepository",
    "ConfidenceAssessment",
    "ConfidenceEvaluator",
    "ConfidenceLevel",
    "ExecutionPlan",
    "IncidentContext",
    "IncidentContextBuilder",
    "IncidentContextError",
    "IncidentSeverity",
    "IncidentStatus",
    "KnowledgeRetrieverPort",
    "PlanStep",
    "RecoveryAction",
    "RecoveryCoordinator",
    "RecoveryExecutor",
    "RecoveryResult",
    "SREAnalyzer",
    "SREPhaseError",
    "SREPlanBuilder",
    "SREWorkflowError",
    "SREWorkflowOrchestrator",
    "SREWorkflowResult",
    "SREWorkflowRuntime",
    "WorkflowAuditor",
    "_make_evaluation_registry",
    "register_sre_workflow",
    "sre_workflow_definition",
]
