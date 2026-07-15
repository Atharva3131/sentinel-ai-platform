"""HealthMonitorAgent — weighted health status aggregation from runtime observations."""

from __future__ import annotations

from typing import Any

from backend.agents.agent import Agent
from backend.agents.context import AgentContext
from backend.agents.models import AgentInput, AgentMetadata, AgentOutput

_DEGRADED_THRESHOLD: float = 0.1
_UNHEALTHY_THRESHOLD: float = 0.5


class HealthMonitorAgent(Agent):
    """Aggregates runtime health observations into a weighted health verdict.

    Input payload:
        observations: list[dict] — each with:
            "component": str
            "error_count": int
            "total_count": int
            "weight": float (default 1.0)
        degraded_threshold: float (default 0.1)
        unhealthy_threshold: float (default 0.5)

    Output payload:
        status: "healthy" | "degraded" | "unhealthy"
        weighted_error_fraction: float
        component_statuses: dict[str, str]
        summary: str
    """

    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            name="health_monitor",
            version="1.0.0",
            description="Aggregates weighted runtime health observations into a health verdict.",
            capabilities=("health", "monitor"),
        )

    async def execute(self, context: AgentContext, agent_input: AgentInput) -> AgentOutput:
        observations: list[dict[str, Any]] = agent_input.payload.get("observations", [])
        degraded_threshold = float(
            agent_input.payload.get("degraded_threshold", _DEGRADED_THRESHOLD)
        )
        unhealthy_threshold = float(
            agent_input.payload.get("unhealthy_threshold", _UNHEALTHY_THRESHOLD)
        )

        if not observations:
            return AgentOutput(
                payload={
                    "status": "healthy",
                    "weighted_error_fraction": 0.0,
                    "component_statuses": {},
                    "summary": "No observations recorded.",
                }
            )

        total_weight = 0.0
        weighted_errors = 0.0
        component_statuses: dict[str, str] = {}

        for obs in observations:
            component: str = obs.get("component", "unknown")
            error_count = int(obs.get("error_count", 0))
            total_count = int(obs.get("total_count", 0))
            weight = float(obs.get("weight", 1.0))

            if total_count == 0:
                component_statuses[component] = "healthy"
                continue

            error_fraction = error_count / total_count
            weighted_errors += error_fraction * weight
            total_weight += weight

            if error_fraction >= unhealthy_threshold:
                component_statuses[component] = "unhealthy"
            elif error_fraction >= degraded_threshold:
                component_statuses[component] = "degraded"
            else:
                component_statuses[component] = "healthy"

        weighted_error_fraction = weighted_errors / total_weight if total_weight > 0 else 0.0

        if weighted_error_fraction >= unhealthy_threshold:
            status = "unhealthy"
        elif weighted_error_fraction >= degraded_threshold:
            status = "degraded"
        else:
            status = "healthy"

        return AgentOutput(
            payload={
                "status": status,
                "weighted_error_fraction": round(weighted_error_fraction, 6),
                "component_statuses": component_statuses,
                "summary": (
                    f"Weighted error fraction: {weighted_error_fraction:.2%}, status: {status}"
                ),
            }
        )
