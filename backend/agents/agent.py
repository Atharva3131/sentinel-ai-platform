"""Agent base class contract."""

from __future__ import annotations

from abc import ABC, abstractmethod

from backend.agents.context import AgentContext
from backend.agents.models import AgentCapabilities, AgentInput, AgentMetadata, AgentOutput


class Agent(ABC):
    """Stateless, async agent interface.

    Agents are expected to be stateless: any per-execution state must live in the input, output,
    and the supplied context. Implementations should be safe to construct per request via DI.
    """

    @property
    @abstractmethod
    def metadata(self) -> AgentMetadata:
        """Return immutable metadata describing the agent."""

    @property
    def name(self) -> str:
        """Return the stable agent name."""
        return self.metadata.name

    @property
    def version(self) -> str | None:
        """Return the agent version, when declared."""
        return self.metadata.version

    @property
    def capabilities(self) -> AgentCapabilities:
        """Return declared agent capabilities."""
        return AgentCapabilities(self.metadata.capabilities)

    @abstractmethod
    async def execute(self, context: AgentContext, agent_input: AgentInput) -> AgentOutput:
        """Execute the agent given the supplied context and input."""
        raise NotImplementedError
