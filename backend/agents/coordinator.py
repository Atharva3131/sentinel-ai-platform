"""CoordinatorAgent — DAG-based agent collaboration readiness checker."""

from __future__ import annotations

from typing import Any

from backend.agents.agent import Agent
from backend.agents.context import AgentContext
from backend.agents.models import AgentInput, AgentMetadata, AgentOutput


class CoordinatorAgent(Agent):
    """Determines which agents are ready to run based on DAG dependency completion.

    Input payload:
        agents: list[dict] — each with "id" (str) and "depends_on" (list[str])
        completed: list[str] — IDs of already-completed agents

    Output payload:
        ready: list[str] — agents whose dependencies are all in `completed`
        blocked: list[str] — agents still waiting on unmet dependencies
        pending_deps: dict[str, list[str]] — agent_id → list of unmet dependency IDs
    """

    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            name="coordinator",
            version="1.0.0",
            description=(
                "Determines which agents are ready to run based on DAG dependency completion."
            ),
            capabilities=("coordinate", "dag"),
        )

    async def execute(self, context: AgentContext, agent_input: AgentInput) -> AgentOutput:
        agents: list[dict[str, Any]] = agent_input.payload.get("agents", [])
        completed: set[str] = set(agent_input.payload.get("completed", []))

        ready: list[str] = []
        blocked: list[str] = []
        pending_deps: dict[str, list[str]] = {}

        for agent in agents:
            agent_id: str = agent["id"]
            if agent_id in completed:
                continue
            unmet = [d for d in agent.get("depends_on", []) if d not in completed]
            if unmet:
                blocked.append(agent_id)
                pending_deps[agent_id] = unmet
            else:
                ready.append(agent_id)

        return AgentOutput(
            payload={"ready": ready, "blocked": blocked, "pending_deps": pending_deps}
        )
