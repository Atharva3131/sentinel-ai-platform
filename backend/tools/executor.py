"""Tool executor with retries, timeouts, permissions, and middleware."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backend.tools.context import ToolContext
from backend.tools.exceptions import ToolException
from backend.tools.middleware.contracts import ToolExecutionRequest
from backend.tools.middleware.pipeline import ToolMiddlewarePipeline
from backend.tools.models import ToolInput, ToolMetadata, ToolResult
from backend.tools.registry import ToolRegistry
from backend.tools.tool import Tool
from backend.tools.validator import ToolValidator


@dataclass(slots=True)
class ToolExecutor:
    """Execute tools with standardized cross-cutting concerns."""

    registry: ToolRegistry
    validator: ToolValidator = field(default_factory=ToolValidator)
    middleware: ToolMiddlewarePipeline = field(default_factory=ToolMiddlewarePipeline)
    default_dependencies: dict[str, Any] = field(default_factory=dict)

    async def execute(
        self,
        name: str,
        *,
        context: ToolContext,
        tool_input: ToolInput,
        version: str | None = None,
        dependencies: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Execute a tool with retries, cancellation, and timeout support."""
        attempt = max(context.attempt, 1)
        max_attempts = max(context.max_attempts, 1)

        while attempt <= max_attempts:
            context_attempt = context.with_attempt(attempt)
            if context_attempt.is_cancelled():
                result = self._cancelled_result(
                    ToolMetadata(name=name, version=version),
                    context_attempt,
                )
                await self._emit(context_attempt, "tool.cancelled", self._event_payload(result))
                return result

            tool = self._create_tool(name, version=version, dependencies=dependencies)
            self.validator.validate_metadata(tool.metadata)
            await self._emit(
                context_attempt,
                "tool.started",
                self._started_payload(tool, context_attempt),
            )

            started_at = datetime.now(UTC)
            try:
                result = await self._execute_once(
                    tool,
                    context_attempt,
                    tool_input,
                    started_at=started_at,
                )
            except ToolException as exc:
                result = self._failed_result(
                    tool.metadata,
                    context_attempt,
                    exc,
                    started_at=started_at,
                )
            except Exception as exc:  # pragma: no cover - defensive
                result = self._failed_result(
                    tool.metadata,
                    context_attempt,
                    ToolException(
                        "Tool execution failed",
                        tool_name=tool.name,
                        tool_version=tool.version,
                        workflow_id=context_attempt.workflow_id,
                        execution_id=context_attempt.execution_id,
                        attempt=context_attempt.attempt,
                        retryable=False,
                        metadata={"exception_type": type(exc).__name__},
                    ),
                    started_at=started_at,
                )

            await self._emit(context_attempt, f"tool.{result.status}", self._event_payload(result))

            if self._should_retry(result, attempt, max_attempts):
                await self._emit(
                    context_attempt,
                    "tool.retried",
                    {
                        **self._event_payload(result),
                        "next_attempt": attempt + 1,
                        "retry_after_seconds": result.retry_after_seconds
                        if result.retry_after_seconds is not None
                        else context_attempt.retry_delay_seconds,
                    },
                )
                delay = result.retry_after_seconds
                if delay is None:
                    delay = context_attempt.retry_delay_seconds
                if delay and delay > 0:
                    await asyncio.sleep(delay)
                attempt += 1
                continue

            return result

        exhausted = ToolException(
            "Tool retry budget exhausted",
            tool_name=name,
            tool_version=version,
            workflow_id=context.workflow_id,
            execution_id=context.execution_id,
            attempt=attempt,
            retryable=False,
        )
        result = self._failed_result(
            ToolMetadata(name=name, version=version),
            context.with_attempt(attempt),
            exhausted,
            started_at=None,
        )
        await self._emit(context, "tool.failed", self._event_payload(result))
        return result

    def _create_tool(
        self,
        name: str,
        *,
        version: str | None,
        dependencies: dict[str, Any] | None,
    ) -> Tool:
        resolved_version, provider = self.registry.resolve(name, version)
        deps = dict(self.default_dependencies)
        if dependencies:
            deps.update(dependencies)
        tool = provider(deps or None)
        if tool.name != name:
            raise ValueError(f"Tool provider returned '{tool.name}', expected '{name}'")
        if tool.version is not None and tool.version != resolved_version:
            raise ValueError(
                f"Tool provider returned version '{tool.version}', expected '{resolved_version}'"
            )
        return tool

    async def _execute_once(
        self,
        tool: Tool,
        context: ToolContext,
        tool_input: ToolInput,
        *,
        started_at: datetime,
    ) -> ToolResult:
        timeout = context.remaining_timeout_seconds()

        async def terminal(request: ToolExecutionRequest) -> ToolResult:
            output = await tool.execute(request.context, request.tool_input)
            ended_at = datetime.now(UTC)
            return ToolResult(
                tool=tool.metadata,
                workflow_id=context.workflow_id,
                execution_id=context.execution_id,
                status="completed",
                output=output,
                retryable=False,
                started_at=started_at,
                ended_at=ended_at,
                metadata=dict(context.metadata),
            )

        handler = self.middleware.wrap(terminal)
        request = ToolExecutionRequest(tool=tool, context=context, tool_input=tool_input)

        async def invoke() -> ToolResult:
            if timeout is None:
                return await handler(request)
            return await asyncio.wait_for(handler(request), timeout=timeout)

        run_task = asyncio.create_task(invoke(), name=f"tool:{tool.name}:{context.execution_id}")

        if context.cancellation_token is None:
            try:
                return await run_task
            except TimeoutError as exc:
                raise ToolException(
                    "Tool execution timed out",
                    tool_name=tool.name,
                    tool_version=tool.version,
                    workflow_id=context.workflow_id,
                    execution_id=context.execution_id,
                    attempt=context.attempt,
                    retryable=True,
                    timeout_seconds=timeout,
                    metadata={"kind": "timeout"},
                ) from exc

        cancel_task = asyncio.create_task(context.cancellation_token.wait(), name="tool:cancel")
        done, _pending = await asyncio.wait(
            {run_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done and not run_task.done():
            run_task.cancel()
            with contextlib.suppress(BaseException):
                await run_task
            raise ToolException(
                "Tool execution cancelled",
                tool_name=tool.name,
                tool_version=tool.version,
                workflow_id=context.workflow_id,
                execution_id=context.execution_id,
                attempt=context.attempt,
                retryable=False,
                metadata={"kind": "cancelled"},
            )
        cancel_task.cancel()
        with contextlib.suppress(BaseException):
            await cancel_task
        try:
            return await run_task
        except TimeoutError as exc:
            raise ToolException(
                "Tool execution timed out",
                tool_name=tool.name,
                tool_version=tool.version,
                workflow_id=context.workflow_id,
                execution_id=context.execution_id,
                attempt=context.attempt,
                retryable=True,
                timeout_seconds=timeout,
                metadata={"kind": "timeout"},
            ) from exc

    def _should_retry(self, result: ToolResult, attempt: int, max_attempts: int) -> bool:
        if attempt >= max_attempts:
            return False
        retryable_statuses = {"failed", "timed_out"}
        return bool(result.retryable) and result.status in retryable_statuses

    def _cancelled_result(self, metadata: ToolMetadata, context: ToolContext) -> ToolResult:
        ended_at = datetime.now(UTC)
        return ToolResult(
            tool=metadata,
            workflow_id=context.workflow_id,
            execution_id=context.execution_id,
            status="cancelled",
            error=ToolException(
                "Cancelled",
                tool_name=metadata.name,
                tool_version=metadata.version,
                workflow_id=context.workflow_id,
                execution_id=context.execution_id,
                attempt=context.attempt,
                retryable=False,
                metadata={"kind": "cancelled"},
            ),
            retryable=False,
            started_at=None,
            ended_at=ended_at,
            metadata=dict(context.metadata),
        )

    def _failed_result(
        self,
        metadata: ToolMetadata,
        context: ToolContext,
        error: ToolException,
        *,
        started_at: datetime | None,
    ) -> ToolResult:
        ended_at = datetime.now(UTC)
        kind = error.metadata.get("kind")
        if kind == "timeout":
            status = "timed_out"
        elif kind == "cancelled":
            status = "cancelled"
        else:
            status = "failed"
        return ToolResult(
            tool=metadata,
            workflow_id=context.workflow_id,
            execution_id=context.execution_id,
            status=status,
            error=error,
            retryable=bool(error.retryable),
            retry_after_seconds=context.retry_delay_seconds if error.retryable else None,
            started_at=started_at,
            ended_at=ended_at,
            metadata={**dict(context.metadata), **dict(error.metadata)},
        )

    def _started_payload(self, tool: Tool, context: ToolContext) -> dict[str, Any]:
        return {
            "tool_name": tool.name,
            "tool_version": tool.version,
            "workflow_id": context.workflow_id,
            "execution_id": context.execution_id,
            "correlation_id": context.correlation_id,
            "attempt": context.attempt,
        }

    def _event_payload(self, result: ToolResult) -> dict[str, Any]:
        return {
            "tool_name": result.tool.name,
            "tool_version": result.tool.version,
            "workflow_id": result.workflow_id,
            "execution_id": result.execution_id,
            "status": str(result.status),
            "retryable": result.retryable,
            "metadata": dict(result.metadata),
        }

    async def _emit(self, context: ToolContext, event_name: str, payload: dict[str, Any]) -> None:
        emitter = context.event_emitter
        if emitter is None:
            return
        try:
            await emitter.emit(event_name, payload, context)
        except Exception:
            return
