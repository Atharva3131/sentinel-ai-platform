"""Composable async middleware pipeline for agent execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from backend.agents.middleware.contracts import (
    AgentExecutionMiddleware,
    AgentExecutionRequest,
)
from backend.agents.models import AgentExecutionResult


@dataclass(slots=True)
class AgentMiddlewarePipeline:
    """Compose middleware around agent execution."""

    middlewares: tuple[AgentExecutionMiddleware, ...] = ()

    def add(self, *middlewares: AgentExecutionMiddleware) -> AgentMiddlewarePipeline:
        """Return a new pipeline with additional middleware appended."""
        return AgentMiddlewarePipeline(middlewares=self.middlewares + middlewares)

    def extend(self, middlewares: Iterable[AgentExecutionMiddleware]) -> AgentMiddlewarePipeline:
        """Return a new pipeline extended with more middleware."""
        return self.add(*tuple(middlewares))

    def wrap(
        self,
        terminal: Callable[[AgentExecutionRequest], Awaitable[AgentExecutionResult]],
    ) -> Callable[[AgentExecutionRequest], Awaitable[AgentExecutionResult]]:
        """Wrap a terminal handler in the configured middleware chain."""
        handler = terminal
        for middleware in reversed(self.middlewares):
            next_handler = handler

            async def wrapped(
                request: AgentExecutionRequest,
                middleware: AgentExecutionMiddleware = middleware,
                next_handler: Callable[
                    [AgentExecutionRequest],
                    Awaitable[AgentExecutionResult],
                ] = next_handler,
            ) -> AgentExecutionResult:
                return await middleware(request, next_handler)

            handler = wrapped
        return handler
