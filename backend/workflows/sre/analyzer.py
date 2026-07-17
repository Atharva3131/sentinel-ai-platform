"""SREAnalyzer — executes analysis tasks using HealthMonitorAgent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agents.context import AgentContext
from backend.agents.lifecycle import AgentStatus
from backend.agents.models import AgentInput
from backend.retrieval.models import RetrievalResult
from backend.workflows.sre.exceptions import SREPhaseError
from backend.workflows.sre.models import (
    AnalysisResult,
    ExecutionPlan,
    IncidentContext,
)
from backend.workflows.sre.ports import AgentRunnerPort

_MAX_FINDING_LENGTH = 200


@dataclass(slots=True)
class SREAnalyzer:
    """Executes the analysis steps from the plan using HealthMonitorAgent.

    The analyzer:
    1. Derives synthetic health observations from the incident's affected services
       (error-state assumed for all services in an active incident).
    2. Calls ``HealthMonitorAgent`` to obtain a weighted health assessment.
    3. Extracts text findings from the top retrieved chunks.
    4. Synthesises a root-cause hypothesis from findings and symptoms.

    Raises:
        SREPhaseError: If the health-monitor agent fails or returns an error status.
    """

    agent_runner: AgentRunnerPort
    max_findings: int = 8

    async def analyze(
        self,
        incident: IncidentContext,
        plan: ExecutionPlan,
        retrieved_results: list[RetrievalResult],
        *,
        context: AgentContext,
    ) -> AnalysisResult:
        """Run analysis and return findings for *incident*.

        Raises:
            SREPhaseError: On health-monitor agent failure.
        """
        observations = self._derive_observations(incident)
        agent_input = AgentInput(payload={"observations": observations})

        try:
            result = await self.agent_runner.execute(
                "health_monitor",
                context=context,
                agent_input=agent_input,
            )
        except Exception as exc:
            raise SREPhaseError(
                f"HealthMonitorAgent failed for incident '{incident.incident_id}': {exc}",
                phase="analysis",
                incident_id=incident.incident_id,
                retryable=True,
            ) from exc

        if result.status not in (AgentStatus.COMPLETED, AgentStatus.FAILED):
            raise SREPhaseError(
                f"HealthMonitorAgent returned unexpected status '{result.status}'",
                phase="analysis",
                incident_id=incident.incident_id,
                retryable=False,
            )

        health_payload: dict[str, Any] = {}
        if result.output is not None and isinstance(result.output.payload, dict):
            health_payload = result.output.payload

        findings = self._extract_findings(retrieved_results, health_payload)
        root_cause = self._hypothesize_root_cause(incident, findings, retrieved_results)

        return AnalysisResult(
            incident_id=incident.incident_id,
            completed_steps=tuple(s.step_id for s in plan.steps),
            failed_steps=(),
            findings=tuple(findings),
            affected_scope=incident.affected_services,
            retrieved_chunk_count=len(retrieved_results),
            root_cause_hypothesis=root_cause,
            graph_depth_reached=None,
            health_status=health_payload.get("status"),
            metadata={"health_assessment": health_payload},
        )

    def _derive_observations(self, incident: IncidentContext) -> list[dict[str, Any]]:
        """Synthesize health observations from affected services.

        All services are assumed to be in an error state because this is an
        active incident. Weights are distributed evenly.
        """
        count = max(len(incident.affected_services), 1)
        weight = round(1.0 / count, 4)
        return [
            {
                "component": service,
                "error_count": 1,
                "total_count": max(10, len(incident.symptoms) + 1),
                "weight": weight,
            }
            for service in incident.affected_services
        ] or [
            {
                "component": "unknown",
                "error_count": 1,
                "total_count": 10,
                "weight": 1.0,
            }
        ]

    def _extract_findings(
        self,
        results: list[RetrievalResult],
        health_payload: dict[str, Any],
    ) -> list[str]:
        findings: list[str] = []
        if summary := health_payload.get("summary"):
            findings.append(str(summary))
        for r in results[: self.max_findings]:
            content = r.chunk.content.strip()
            if content:
                truncated = content[:_MAX_FINDING_LENGTH]
                if len(content) > _MAX_FINDING_LENGTH:
                    truncated += "…"
                findings.append(truncated)
        return findings

    def _hypothesize_root_cause(
        self,
        incident: IncidentContext,
        findings: list[str],
        results: list[RetrievalResult],
    ) -> str | None:
        if not findings and not incident.symptoms:
            return None
        evidence_count = len(results)
        services = ", ".join(incident.affected_services) or "unknown services"
        if incident.symptoms:
            symptom_summary = "; ".join(incident.symptoms[:3])
            return (
                f"Observed symptoms ({symptom_summary}) across {services}. "
                f"Based on {evidence_count} evidence items."
            )
        if findings:
            return (
                f"Inferred from {evidence_count} knowledge-base matches "
                f"for {services}: {findings[0][:100]}"
            )
        return f"Insufficient evidence to determine root cause for {services}."
