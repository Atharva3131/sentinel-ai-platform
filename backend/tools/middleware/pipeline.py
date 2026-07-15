"""Composable async middleware pipeline for tool execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from backend.tools.middleware.contracts import ToolExecutionMiddleware, ToolExecutionRequest
from backend.tools.models import ToolResult


@dataclass(slots=True)
class ToolMiddlewarePipeline:
    """Compose middleware around tool execution."""

    middlewares: tuple[ToolExecutionMiddleware, ...] = ()

    def add(self, *middlewares: ToolExecutionMiddleware) -> ToolMiddlewarePipeline:
        """Return a new pipeline with middleware appended."""
        return ToolMiddlewarePipeline(middlewares=self.middlewares + middlewares)

    def extend(self, middlewares: Iterable[ToolExecutionMiddleware]) -> ToolMiddlewarePipeline:
        """Return a new pipeline extended with more middleware."""
        return self.add(*tuple(middlewares))

    def wrap(
        self,
        terminal: Callable[[ToolExecutionRequest], Awaitable[ToolResult]],
    ) -> Callable[[ToolExecutionRequest], Awaitable[ToolResult]]:
        """Wrap a terminal handler in the configured middleware chain."""
        handler = terminal
        for middleware in reversed(self.middlewares):
            next_handler = handler

            async def wrapped(
                request: ToolExecutionRequest,
                middleware: ToolExecutionMiddleware = middleware,
                next_handler: Callable[
                    [ToolExecutionRequest],
                    Awaitable[ToolResult],
                ] = next_handler,
            ) -> ToolResult:
                return await middleware(request, next_handler)

            handler = wrapped
        return handler
