"""PlannerAgent — builds deterministic execution plans via topological sort."""

from __future__ import annotations

from collections import deque
from typing import Any

from backend.agents.agent import Agent
from backend.agents.context import AgentContext
from backend.agents.models import AgentInput, AgentMetadata, AgentOutput


class PlannerAgent(Agent):
    """Builds ordered execution plans from task dependency graphs.

    Input payload:
        tasks: list[dict] — each dict has "id" (str) and "depends_on" (list[str])

    Output payload:
        plan: list[str] — topologically sorted task IDs (Kahn's algorithm)
        stages: list[list[str]] — tasks grouped by parallel-eligible wave
        cycle_detected: bool — True when the dependency graph contains a cycle
    """

    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            name="planner",
            version="1.0.0",
            description="Builds deterministic execution plans from task dependency graphs.",
            capabilities=("plan", "topology"),
        )

    async def execute(self, context: AgentContext, agent_input: AgentInput) -> AgentOutput:
        tasks: list[dict[str, Any]] = agent_input.payload.get("tasks", [])
        if not tasks:
            return AgentOutput(payload={"plan": [], "stages": [], "cycle_detected": False})
        plan, stages, cycle = self._topological_sort(tasks)
        return AgentOutput(payload={"plan": plan, "stages": stages, "cycle_detected": cycle})

    def _topological_sort(
        self, tasks: list[dict[str, Any]]
    ) -> tuple[list[str], list[list[str]], bool]:
        task_ids = {t["id"] for t in tasks}
        indegree: dict[str, int] = {t["id"]: 0 for t in tasks}
        adjacency: dict[str, list[str]] = {t["id"]: [] for t in tasks}

        for task in tasks:
            for dep in task.get("depends_on", []):
                if dep in task_ids:
                    adjacency[dep].append(task["id"])
                    indegree[task["id"]] += 1

        queue: deque[str] = deque(tid for tid, deg in indegree.items() if deg == 0)
        plan: list[str] = []
        stages: list[list[str]] = []

        while queue:
            stage = list(queue)
            queue.clear()
            stages.append(stage)
            for tid in stage:
                plan.append(tid)
                for neighbor in adjacency[tid]:
                    indegree[neighbor] -= 1
                    if indegree[neighbor] == 0:
                        queue.append(neighbor)

        if len(plan) != len(task_ids):
            return [], [], True

        return plan, stages, False
