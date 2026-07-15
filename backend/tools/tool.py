"""Tool interface contract."""

from __future__ import annotations

from abc import ABC, abstractmethod

from backend.tools.context import ToolContext
from backend.tools.models import ToolInput, ToolMetadata


class Tool(ABC):
    """Stateless, async tool interface."""

    @property
    @abstractmethod
    def metadata(self) -> ToolMetadata:
        """Return immutable metadata describing the tool."""

    @property
    def name(self) -> str:
        """Return the stable tool name."""
        return self.metadata.name

    @property
    def version(self) -> str | None:
        """Return the tool version, when declared."""
        return self.metadata.version

    @abstractmethod
    async def execute(self, context: ToolContext, tool_input: ToolInput) -> object:
        """Execute the tool given the supplied context and input."""
        raise NotImplementedError
