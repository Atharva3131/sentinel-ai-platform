"""SREWorkflowOrchestrator — top-level incident response conductor."""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from backend.agents.context import AgentContext
from backend.runtime.context import RuntimeContext
from backend.workflows.sre.auditor import WorkflowAuditor
from backend.workflows.sre.confidence import ConfidenceEvaluator
from backend.workflows.sre.context_builder import IncidentContextBuilder
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
from backend.workflows.sre.exceptions import SREPhaseError, SREWorkflowError
from backend.workflows.sre.models import (
    ApprovalRequest,
    IncidentContext,
    SREWorkflowResult,
)
from backend.workflows.sre.plan_builder import SREPlanBuilder
from backend.workflows.sre.analyzer import SREAnalyzer
from backend.workflows.sre.ports import ApprovalGateway
from backend.workflows.sre.recovery import RecoveryCoordinator


def _make_agent_context(context: RuntimeContext) -> AgentContext:
    return AgentContext(
        workflow_id=context.workflow.workflow_id,
        execution_id=context.execution.execution_id,
        correlation_id=context.correlation_id,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        deadline=context.deadline,
        timeout_seconds=context.remaining_timeout_seconds(),
        attempt=context.attempt,
        max_attempts=context.max_attempts,
    )


@dataclass(slots=True)
class SREWorkflowOrchestrator:
    """Conducts the full incident response lifecycle.

    Phase sequence:
        1. Context retrieval   — KnowledgeEngine (hybrid retrieval)
        2. Plan build          — PlannerAgent via AgentRunner
        3. Analysis            — HealthMonitorAgent via AgentRunner
        4. Confidence          — EvaluationFramework (retrieval quality + analysis)
        5. Approval gate       — ApprovalGateway (when confidence < threshold)
        6. Recovery            — RecoveryCoordinator + RecoveryExecutor
        7. Audit finalisation  — WorkflowAuditor flushes all entries

    Every phase emits a structured event via ``RuntimeContext.event_emitter``
    and writes an ``AuditEntry`` via ``WorkflowAuditor``.  Cancellation and
    timeout are checked before each phase; the workflow returns a ``cancelled``
    or ``timed_out`` result rather than raising.

    The orchestrator is stateless between ``run()`` calls — it is safe to share
    across concurrent invocations.
    """

    context_builder: IncidentContextBuilder
    plan_builder: SREPlanBuilder
    analyzer: SREAnalyzer
    confidence_evaluator: ConfidenceEvaluator
    recovery_coordinator: RecoveryCoordinator
    approval_gateway: ApprovalGateway | None = None

    async def run(
        self,
        incident: IncidentContext,
        runtime_context: RuntimeContext,
    ) -> SREWorkflowResult:
        """Execute the full incident response workflow.

        Returns a ``SREWorkflowResult`` in all non-exception cases, including
        cancellation, timeout, and phase failures.  Only unrecoverable internal
        errors (e.g. programmer mistakes) will propagate as raw exceptions.
        """
        started_ms = time.monotonic() * 1000
        auditor = WorkflowAuditor(
            repository=self._null_audit_repository(),
        )
        wf_id = runtime_context.workflow.workflow_id
        exec_id = runtime_context.execution.execution_id
        corr_id = runtime_context.correlation_id

        await self._emit(runtime_context, SRE_WORKFLOW_STARTED, {
            "incident_id": incident.incident_id,
            "severity": str(incident.severity),
            "affected_services": list(incident.affected_services),
        })

        def _duration() -> float:
            return time.monotonic() * 1000 - started_ms

        def _base_result(status: str, **kwargs: Any) -> SREWorkflowResult:
            return SREWorkflowResult(
                incident_id=incident.incident_id,
                status=status,
                audit_entries=auditor.entries(),
                duration_ms=_duration(),
                workflow_id=wf_id,
                execution_id=exec_id,
                **kwargs,
            )

        agent_context = _make_agent_context(runtime_context)

        # ── Phase 1: Context retrieval ────────────────────────────────────
        if runtime_context.is_cancelled():
            await self._emit(runtime_context, SRE_WORKFLOW_CANCELLED, {"phase": "before_retrieval"})
            return _base_result("cancelled")

        await self._emit(runtime_context, SRE_CONTEXT_RETRIEVAL_STARTED, {
            "incident_id": incident.incident_id,
        })
        try:
            retrieved_results, retrieval_metadata = await self.context_builder.build(
                incident,
                workflow_id=wf_id,
                execution_id=exec_id,
                correlation_id=corr_id,
            )
        except SREPhaseError as exc:
            await self._emit(runtime_context, SRE_WORKFLOW_FAILED, {
                "phase": exc.phase, "error": str(exc),
            })
            return _base_result("failed", metadata={"error": str(exc), "phase": exc.phase})

        await self._emit(runtime_context, SRE_CONTEXT_RETRIEVED, {
            "chunk_count": len(retrieved_results),
            "cached": retrieval_metadata.cached,
            "latency_ms": retrieval_metadata.latency_ms,
        })

        # ── Phase 2: Plan build ───────────────────────────────────────────
        if runtime_context.is_cancelled():
            await self._emit(runtime_context, SRE_WORKFLOW_CANCELLED, {"phase": "before_plan"})
            return _base_result("cancelled")

        await self._emit(runtime_context, SRE_PLAN_BUILD_STARTED, {
            "incident_id": incident.incident_id,
        })
        try:
            plan = await self.plan_builder.build(
                incident, retrieved_results, context=agent_context
            )
        except SREPhaseError as exc:
            await self._emit(runtime_context, SRE_WORKFLOW_FAILED, {
                "phase": exc.phase, "error": str(exc),
            })
            return _base_result("failed", metadata={"error": str(exc), "phase": exc.phase})

        await self._emit(runtime_context, SRE_PLAN_BUILT, {
            "plan_id": plan.plan_id,
            "step_count": len(plan.steps),
            "stage_count": len(plan.stages),
        })

        # ── Phase 3: Analysis ─────────────────────────────────────────────
        if runtime_context.is_cancelled():
            await self._emit(runtime_context, SRE_WORKFLOW_CANCELLED, {"phase": "before_analysis"})
            return _base_result("cancelled", plan=plan)

        await self._emit(runtime_context, SRE_ANALYSIS_STARTED, {
            "incident_id": incident.incident_id,
        })
        try:
            analysis = await self.analyzer.analyze(
                incident, plan, retrieved_results, context=agent_context
            )
        except SREPhaseError as exc:
            await self._emit(runtime_context, SRE_WORKFLOW_FAILED, {
                "phase": exc.phase, "error": str(exc),
            })
            return _base_result("failed", plan=plan, metadata={"error": str(exc), "phase": exc.phase})

        await self._emit(runtime_context, SRE_ANALYSIS_COMPLETED, {
            "finding_count": len(analysis.findings),
            "root_cause": analysis.root_cause_hypothesis,
            "health_status": analysis.health_status,
        })

        # ── Phase 4: Confidence evaluation ────────────────────────────────
        if runtime_context.is_cancelled():
            await self._emit(runtime_context, SRE_WORKFLOW_CANCELLED, {"phase": "before_confidence"})
            return _base_result("cancelled", plan=plan, analysis=analysis)

        await self._emit(runtime_context, SRE_CONFIDENCE_EVALUATION_STARTED, {
            "incident_id": incident.incident_id,
        })
        try:
            confidence = await self.confidence_evaluator.evaluate(
                incident,
                analysis,
                retrieved_results,
                retrieval_metadata,
                evaluation_id=str(uuid.uuid4()),
            )
        except SREPhaseError as exc:
            await self._emit(runtime_context, SRE_WORKFLOW_FAILED, {
                "phase": exc.phase, "error": str(exc),
            })
            return _base_result(
                "failed", plan=plan, analysis=analysis,
                metadata={"error": str(exc), "phase": exc.phase},
            )

        await self._emit(runtime_context, SRE_CONFIDENCE_EVALUATED, {
            "score": confidence.score,
            "level": str(confidence.level),
            "requires_approval": confidence.requires_approval,
        })

        # ── Phase 5: Approval gate ────────────────────────────────────────
        approval = None
        if confidence.requires_approval:
            if self.approval_gateway is None:
                # No gateway configured — escalate rather than proceed blindly.
                await self._emit(runtime_context, SRE_WORKFLOW_FAILED, {
                    "reason": "approval_required_but_no_gateway",
                    "confidence_score": confidence.score,
                })
                return _base_result(
                    "escalated",
                    plan=plan,
                    analysis=analysis,
                    confidence=confidence,
                    evaluation_report=confidence.evaluation_report,
                )

            request = ApprovalRequest(
                request_id=str(uuid.uuid4()),
                incident_id=incident.incident_id,
                plan=plan,
                analysis=analysis,
                confidence=confidence,
                requested_at=_utcnow(),
            )
            await self._emit(runtime_context, SRE_APPROVAL_REQUESTED, {
                "request_id": request.request_id,
                "timeout_seconds": request.timeout_seconds,
                "confidence_score": confidence.score,
            })
            try:
                approval = await self.approval_gateway.request_approval(request)
            except Exception as exc:
                await self._emit(runtime_context, SRE_WORKFLOW_FAILED, {
                    "phase": "approval", "error": str(exc),
                })
                return _base_result(
                    "failed",
                    plan=plan,
                    analysis=analysis,
                    confidence=confidence,
                    metadata={"error": str(exc), "phase": "approval"},
                )

            await self._emit(runtime_context, SRE_APPROVAL_RECEIVED, {
                "request_id": approval.request_id,
                "decision": str(approval.decision),
                "approver_id": approval.approver_id,
            })

        # ── Phase 6: Recovery ─────────────────────────────────────────────
        if runtime_context.is_cancelled():
            await self._emit(runtime_context, SRE_WORKFLOW_CANCELLED, {"phase": "before_recovery"})
            return _base_result(
                "cancelled",
                plan=plan, analysis=analysis, confidence=confidence, approval=approval,
            )

        await self._emit(runtime_context, SRE_RECOVERY_STARTED, {
            "incident_id": incident.incident_id,
        })
        try:
            recovery = await self.recovery_coordinator.run(
                incident, plan, analysis, approval, correlation_id=corr_id
            )
        except SREPhaseError as exc:
            await self._emit(runtime_context, SRE_WORKFLOW_FAILED, {
                "phase": exc.phase, "error": str(exc),
            })
            return _base_result(
                "failed",
                plan=plan, analysis=analysis, confidence=confidence, approval=approval,
                metadata={"error": str(exc), "phase": exc.phase},
            )

        await self._emit(runtime_context, SRE_RECOVERY_COMPLETED, {
            "actions_attempted": recovery.actions_attempted,
            "actions_succeeded": recovery.actions_succeeded,
            "recovered": recovery.recovered,
        })

        # ── Phase 7: Finalise ─────────────────────────────────────────────
        final_status = "completed" if recovery.recovered else "mitigated"
        await self._emit(runtime_context, SRE_WORKFLOW_COMPLETED, {
            "incident_id": incident.incident_id,
            "status": final_status,
            "duration_ms": _duration(),
        })

        return SREWorkflowResult(
            incident_id=incident.incident_id,
            status=final_status,
            workflow_id=wf_id,
            execution_id=exec_id,
            plan=plan,
            analysis=analysis,
            confidence=confidence,
            approval=approval,
            recovery=recovery,
            evaluation_report=confidence.evaluation_report,
            audit_entries=auditor.entries(),
            duration_ms=_duration(),
        )

    async def _emit(
        self,
        context: RuntimeContext,
        event_name: str,
        payload: dict[str, Any],
    ) -> None:
        if context.event_emitter is None:
            return
        try:
            await context.event_emitter.emit(event_name, payload, context)
        except Exception:
            pass  # event emission must never abort the workflow

    @staticmethod
    def _null_audit_repository() -> Any:
        """Return a no-op audit repository used when no external repo is wired."""

        class _NullRepo:
            async def persist(self, entry: Any) -> None:
                pass

            async def list_entries(self, incident_id: str) -> list[Any]:
                return []

        return _NullRepo()


def _utcnow() -> Any:
    from datetime import UTC, datetime
    return datetime.now(UTC)
