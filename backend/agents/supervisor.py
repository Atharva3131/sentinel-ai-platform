"""SupervisorAgent — monitors workflows for deadline/stall/loop/budget failures."""

from __future__ import annotations

from datetime import UTC, datetime

from backend.agents.agent import Agent
from backend.agents.context import AgentContext
from backend.agents.models import AgentInput, AgentMetadata, AgentOutput

_DEFAULT_STALL_THRESHOLD_SECONDS: float = 300.0
_DEFAULT_LOOP_THRESHOLD: int = 10


class SupervisorAgent(Agent):
    """Monitors a workflow execution and returns a supervision verdict.

    Input payload:
        workflow_id: str
        deadline: str | None — ISO-8601 deadline timestamp
        last_activity_at: str | None — ISO-8601 last-activity timestamp
        budget_used: float — resources consumed (abstract units)
        budget_limit: float | None — max allowed budget
        state_visit_counts: dict[str, int] — state_name → visit count
        stall_threshold_seconds: float (default 300)
        loop_threshold: int (default 10)

    Output payload:
        verdict: "ok" | "deadline_exceeded" | "stall_detected" | "loop_detected" | "budget_exceeded"
        reason: str
        workflow_id: str
    """

    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            name="supervisor",
            version="1.0.0",
            description=(
                "Monitors workflow execution for deadline, stall, loop, and budget failures."
            ),
            capabilities=("supervise", "monitor"),
        )

    async def execute(self, context: AgentContext, agent_input: AgentInput) -> AgentOutput:
        p = agent_input.payload
        workflow_id: str = p.get("workflow_id", "unknown")
        now = datetime.now(UTC)

        deadline_raw: str | None = p.get("deadline")
        if deadline_raw:
            deadline = datetime.fromisoformat(deadline_raw)
            if now >= deadline:
                return self._verdict(
                    workflow_id, "deadline_exceeded", f"Deadline {deadline_raw} exceeded"
                )

        stall_threshold = float(
            p.get("stall_threshold_seconds", _DEFAULT_STALL_THRESHOLD_SECONDS)
        )
        last_activity_raw: str | None = p.get("last_activity_at")
        if last_activity_raw:
            last_activity = datetime.fromisoformat(last_activity_raw)
            idle = (now - last_activity).total_seconds()
            if idle > stall_threshold:
                return self._verdict(
                    workflow_id, "stall_detected",
                    f"No activity for {idle:.0f}s (threshold {stall_threshold:.0f}s)",
                )

        loop_threshold = int(p.get("loop_threshold", _DEFAULT_LOOP_THRESHOLD))
        state_visits: dict[str, int] = p.get("state_visit_counts", {})
        for state, count in state_visits.items():
            if count >= loop_threshold:
                return self._verdict(
                    workflow_id, "loop_detected",
                    f"State '{state}' visited {count} times (threshold {loop_threshold})",
                )

        budget_limit = p.get("budget_limit")
        if budget_limit is not None:
            budget_used = float(p.get("budget_used", 0.0))
            if budget_used >= float(budget_limit):
                return self._verdict(
                    workflow_id, "budget_exceeded",
                    f"Budget used {budget_used} >= limit {budget_limit}",
                )

        return self._verdict(workflow_id, "ok", "Workflow within operational bounds")

    def _verdict(self, workflow_id: str, verdict: str, reason: str) -> AgentOutput:
        return AgentOutput(
            payload={"verdict": verdict, "reason": reason, "workflow_id": workflow_id}
        )
