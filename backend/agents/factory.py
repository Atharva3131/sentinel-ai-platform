"""Agent factory for dependency-injected agent instances."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.agents.agent import Agent
from backend.agents.registry import AgentRegistry


@dataclass(slots=True)
class AgentFactory:
    """Create stateless agent instances from a registry of providers."""

    registry: AgentRegistry
    default_dependencies: dict[str, Any] = field(default_factory=dict)

    def create(
        self,
        name: str,
        *,
        version: str | None = None,
        dependencies: dict[str, Any] | None = None,
    ) -> Agent:
        """Create a new agent instance by name and optional version."""
        resolved_version, provider = self.registry.resolve(name, version)
        deps = dict(self.default_dependencies)
        if dependencies:
            deps.update(dependencies)
        agent = provider(deps or None)
        if agent.name != name:
            raise ValueError(
                f"Agent provider returned '{agent.name}', expected '{name}'",
            )
        if agent.version is not None and agent.version != resolved_version:
            raise ValueError(
                f"Agent provider returned version '{agent.version}', expected '{resolved_version}'",
            )
        return agent
