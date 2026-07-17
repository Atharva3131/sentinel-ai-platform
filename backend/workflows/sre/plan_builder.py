"""SREPlanBuilder — builds a topologically-sorted execution plan via PlannerAgent."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.agents.context import AgentContext
from backend.agents.models import AgentInput
from backend.retrieval.models import RetrievalResult
from backend.workflows.sre.exceptions import SREPhaseError
from backend.workflows.sre.models import (
    ExecutionPlan,
    IncidentContext,
    IncidentSeverity,
    PlanStep,
)
from backend.workflows.sre.ports import AgentRunnerPort

_BASE_TASKS: list[dict[str, Any]] = [
    {"id": "analyze_symptoms", "depends_on": [], "agent": "health_monitor"},
    {"id": "assess_scope", "depends_on": ["analyze_symptoms"], "agent": "health_monitor"},
    {"id": "identify_root_cause", "depends_on": ["assess_scope"], "agent": "health_monitor"},
]

_CRITICAL_TASKS: list[dict[str, Any]] = [
    {"id": "notify_oncall", "depends_on": ["analyze_symptoms"], "agent": "supervisor"},
]

_RUNBOOK_TASK: dict[str, Any] = {
    "id": "apply_runbook",
    "depends_on": ["identify_root_cause"],
    "agent": "health_monitor",
}


@dataclass(slots=True)
class SREPlanBuilder:
    """Derives SRE-specific tasks from the incident and delegates ordering to PlannerAgent.

    Task derivation is deterministic: base tasks are always included; additional
    tasks are appended based on severity and whether runbooks were retrieved.
    The PlannerAgent performs topological sort and detects cycles.

    Raises:
        SREPhaseError: If the planner agent fails or returns a cycle.
    """

    agent_runner: AgentRunnerPort

    async def build(
        self,
        incident: IncidentContext,
        retrieved_results: list[RetrievalResult],
        *,
        context: AgentContext,
    ) -> ExecutionPlan:
        """Build an execution plan for *incident*.

        Args:
            incident: The incident being investigated.
            retrieved_results: Context retrieved from the knowledge engine;
                used to detect available runbooks.
            context: Agent execution context.

        Returns:
            A fully-ordered ``ExecutionPlan``.

        Raises:
            SREPhaseError: On planner agent failure or cycle detection.
        """
        tasks = self._derive_tasks(incident, retrieved_results)
        agent_input = AgentInput(payload={"tasks": tasks})

        try:
            result = await self.agent_runner.execute(
                "planner",
                context=context,
                agent_input=agent_input,
            )
        except Exception as exc:
            raise SREPhaseError(
                f"PlannerAgent failed for incident '{incident.incident_id}': {exc}",
                phase="plan_build",
                incident_id=incident.incident_id,
                retryable=True,
            ) from exc

        payload: dict[str, Any] = {}
        if result.output is not None:
            payload = result.output.payload if isinstance(result.output.payload, dict) else {}

        if payload.get("cycle_detected"):
            raise SREPhaseError(
                f"Cycle detected in execution plan for incident '{incident.incident_id}'",
                phase="plan_build",
                incident_id=incident.incident_id,
                retryable=False,
            )

        return self._assemble_plan(incident.incident_id, tasks, payload)

    def _derive_tasks(
        self,
        incident: IncidentContext,
        results: list[RetrievalResult],
    ) -> list[dict[str, Any]]:
        tasks = list(_BASE_TASKS)
        if incident.severity in (IncidentSeverity.CRITICAL, IncidentSeverity.HIGH):
            tasks.extend(_CRITICAL_TASKS)
        has_runbooks = any(
            "runbook" in (r.chunk.content.lower() + (r.provenance or "").lower())
            for r in results
        )
        if has_runbooks:
            tasks.append(_RUNBOOK_TASK)
        return tasks

    def _assemble_plan(
        self,
        incident_id: str,
        tasks: list[dict[str, Any]],
        planner_output: dict[str, Any],
    ) -> ExecutionPlan:
        plan_order: list[str] = planner_output.get("plan", [t["id"] for t in tasks])
        raw_stages: list[list[str]] = planner_output.get("stages", [plan_order])
        task_by_id = {t["id"]: t for t in tasks}
        steps = tuple(
            PlanStep(
                step_id=tid,
                name=tid.replace("_", " ").title(),
                agent_name=task_by_id.get(tid, {}).get("agent", "health_monitor"),
                depends_on=tuple(task_by_id.get(tid, {}).get("depends_on", [])),
            )
            for tid in plan_order
            if tid in task_by_id
        )
        stages = tuple(tuple(stage) for stage in raw_stages)
        return ExecutionPlan(
            plan_id=str(uuid.uuid4()),
            incident_id=incident_id,
            steps=steps,
            stages=stages,
            created_at=datetime.now(UTC),
        )
