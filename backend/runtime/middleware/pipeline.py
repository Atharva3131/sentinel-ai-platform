"""Composable async middleware pipeline for runtime execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from backend.runtime.contracts import WorkflowRuntime
from backend.runtime.middleware.contracts import (
    RuntimeExecutionMiddleware,
    RuntimeExecutionRequest,
)
from backend.runtime.results import RuntimeResult


@dataclass(slots=True)
class RuntimeMiddlewarePipeline:
    """Compose middleware around workflow runtime execution."""

    middlewares: tuple[RuntimeExecutionMiddleware, ...] = ()
    runtime_attributes: dict[str, Any] = field(default_factory=dict)

    def add(
        self,
        *middlewares: RuntimeExecutionMiddleware,
    ) -> RuntimeMiddlewarePipeline:
        """Return a new pipeline with additional middleware appended."""
        return RuntimeMiddlewarePipeline(
            middlewares=self.middlewares + middlewares,
            runtime_attributes=dict(self.runtime_attributes),
        )

    def extend(
        self,
        middlewares: Iterable[RuntimeExecutionMiddleware],
    ) -> RuntimeMiddlewarePipeline:
        """Return a new pipeline extended with more middleware."""
        return self.add(*tuple(middlewares))

    async def execute(
        self,
        runtime: WorkflowRuntime,
        request: RuntimeExecutionRequest,
    ) -> RuntimeResult:
        """Execute the runtime through the configured middleware chain."""
        return await self._wrap_terminal(runtime)(request)

    def _wrap_terminal(
        self,
        runtime: WorkflowRuntime,
    ) -> Callable[[RuntimeExecutionRequest], Awaitable[RuntimeResult]]:
        async def terminal(request: RuntimeExecutionRequest) -> RuntimeResult:
            return await runtime.execute(request.context)

        handler: Callable[[RuntimeExecutionRequest], Awaitable[RuntimeResult]] = terminal
        for middleware in reversed(self.middlewares):
            next_handler = handler

            async def wrapped(
                request: RuntimeExecutionRequest,
                middleware: RuntimeExecutionMiddleware = middleware,
                next_handler: Callable[
                    [RuntimeExecutionRequest],
                    Awaitable[RuntimeResult],
                ] = next_handler,
            ) -> RuntimeResult:
                return await middleware(request, next_handler)

            handler = wrapped
        return handler
