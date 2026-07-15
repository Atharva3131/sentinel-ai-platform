"""LangGraph runtime adapter implementation.

The adapter isolates LangGraph behind a small engine boundary. The runtime core and services must
not import LangGraph types.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from backend.runtime import RuntimeContext, RuntimeException, RuntimeResult, WorkflowRuntime


class LangGraphEngine(Protocol):
    """Minimal engine surface used by the adapter to invoke a compiled LangGraph app."""

    async def ainvoke(
        self,
        app: Any,
        *,
        state: Mapping[str, Any],
        config: Mapping[str, Any] | None = None,
    ) -> Any:
        """Invoke the compiled graph asynchronously."""
        ...


def _default_workflow_resolver(workflow_metadata: Any) -> Any:
    """Resolve the LangGraph app from workflow metadata.

    Convention:
    - `workflow_metadata.metadata["langgraph_app"]` holds a compiled app object compatible with
      `LangGraphEngine.ainvoke`.
    """

    if not hasattr(workflow_metadata, "metadata"):
        raise RuntimeException("Workflow metadata is missing `metadata` mapping")
    metadata = workflow_metadata.metadata
    if not isinstance(metadata, dict):
        raise RuntimeException("Workflow metadata `metadata` must be a dict")
    try:
        return metadata["langgraph_app"]
    except KeyError as exc:
        raise RuntimeException(
            "LangGraph workflow requires `workflow.metadata['langgraph_app']`",
            metadata={"required_key": "langgraph_app"},
        ) from exc


class DefaultLangGraphEngine:
    """Default engine that delegates to LangGraph-compatible compiled graph APIs.

    This class intentionally avoids importing LangGraph at module import time. If the concrete app
    implements `ainvoke` (LangGraph compiled graphs do), the engine can call it without depending on
    LangGraph symbols.
    """

    async def ainvoke(
        self,
        app: Any,
        *,
        state: Mapping[str, Any],
        config: Mapping[str, Any] | None = None,
    ) -> Any:
        ainvoke = getattr(app, "ainvoke", None)
        if ainvoke is None:
            raise RuntimeException("LangGraph app does not expose async `ainvoke`")
        result = ainvoke(state, config=config) if config is not None else ainvoke(state)
        if isinstance(result, Awaitable):
            return await result
        raise RuntimeException("LangGraph app `ainvoke` did not return an awaitable")


@dataclass(slots=True)
class _ExecutionState:
    task: asyncio.Task[RuntimeResult]
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class LangGraphRuntime(WorkflowRuntime):
    """WorkflowRuntime adapter for LangGraph.

    Responsibilities:
    - Translate `RuntimeContext` into LangGraph state/config payloads.
    - Invoke the compiled LangGraph app asynchronously through an injected engine.
    - Convert outputs and failures into `RuntimeResult` and `RuntimeException`.
    - Provide cancellation hooks and emit runtime lifecycle events.
    """

    workflow_resolver: Callable[[Any], Any] = _default_workflow_resolver
    engine: LangGraphEngine = field(default_factory=DefaultLangGraphEngine)
    runtime_name: str = "langgraph"
    runtime_version: str | None = None
    supported_capabilities: tuple[str, ...] = ("langgraph",)
    _executions: dict[str, _ExecutionState] = field(default_factory=dict, init=False)

    @property
    def name(self) -> str:
        return self.runtime_name

    @property
    def version(self) -> str | None:
        return self.runtime_version

    @property
    def capabilities(self) -> tuple[str, ...]:
        return self.supported_capabilities

    def supports(self, workflow: Any) -> bool:
        """Return `True` if the workflow is compatible with this adapter.

        This adapter stays intentionally permissive. When a workflow declares a required runtime via
        `workflow.metadata["required_runtime"]`, this method enforces it. Otherwise the registry
        name selection is the primary mechanism for choosing the runtime.
        """

        metadata = getattr(workflow, "metadata", None)
        if isinstance(metadata, dict):
            required = metadata.get("required_runtime")
            if required is not None:
                if not isinstance(required, str):
                    return False
                return required == self.name
        return True

    async def execute(self, context: RuntimeContext) -> RuntimeResult:
        """Execute the workflow using the LangGraph app resolved from the workflow metadata."""

        async def runner() -> RuntimeResult:
            return await self._execute_with_retries(context)

        execution_id = context.execution.execution_id
        task = asyncio.create_task(runner(), name=f"langgraph:{execution_id}")
        self._executions[execution_id] = _ExecutionState(task=task)
        try:
            try:
                return await task
            except asyncio.CancelledError:
                return await self._cancelled_result(context)
        finally:
            self._executions.pop(execution_id, None)

    async def cancel(self, context: RuntimeContext) -> None:
        """Cancel an in-flight execution task when present."""
        execution_id = context.execution.execution_id
        state = self._executions.get(execution_id)
        if state is None:
            return
        if not state.task.done():
            state.task.cancel()

    async def _execute_with_retries(self, context: RuntimeContext) -> RuntimeResult:
        try:
            attempts = max(context.max_attempts, 1)
            for attempt in range(1, attempts + 1):
                if context.is_cancelled():
                    return await self._cancelled_result(context)
                if context.has_timed_out():
                    return await self._timeout_result(context)

                context_attempt = self._with_attempt(context, attempt)
                await self._emit(
                    context_attempt,
                    "runtime.langgraph.attempt_started",
                    {"attempt": attempt},
                )
                result = await self._execute_once(context_attempt)
                if result.status == "completed":
                    return result
                if result.status == "cancelled":
                    return result
                if result.error is None or not result.retryable or attempt >= attempts:
                    return result

                delay = context_attempt.retry_delay_seconds or 0.0
                await self._emit(
                    context_attempt,
                    "runtime.langgraph.retry_scheduled",
                    {"attempt": attempt, "retry_after_seconds": delay},
                )
                if delay > 0:
                    await asyncio.sleep(delay)
            return await self._failed_result(
                context,
                RuntimeException("Runtime attempts exhausted"),
            )
        except asyncio.CancelledError:
            return await self._cancelled_result(context)

    async def _execute_once(self, context: RuntimeContext) -> RuntimeResult:
        execution = context.execution
        workflow = context.workflow
        started_at = datetime.now(UTC)
        await self._emit(
            context,
            "runtime.langgraph.started",
            {"runtime": self.name, "execution_id": execution.execution_id},
        )

        try:
            app = self.workflow_resolver(workflow)
            state = self._context_to_state(context)
            config = self._context_to_config(context)

            timeout = context.remaining_timeout_seconds()
            if timeout is not None:
                output = await asyncio.wait_for(
                    self.engine.ainvoke(app, state=state, config=config),
                    timeout=timeout,
                )
            else:
                output = await self.engine.ainvoke(app, state=state, config=config)

            ended_at = datetime.now(UTC)
            updated_execution = replace(
                execution,
                started_at=execution.started_at or started_at,
                ended_at=ended_at,
            )
            await self._emit(
                context,
                "runtime.langgraph.completed",
                {"duration_ms": _duration_ms(started_at, ended_at)},
            )
            return RuntimeResult(
                workflow=workflow,
                execution=updated_execution,
                status="completed",
                output=self._langgraph_output_to_result(output),
                emitted_events=("runtime.langgraph.completed",),
            )
        except TimeoutError:
            await self._emit(
                context,
                "runtime.langgraph.timeout",
                {"execution_id": execution.execution_id},
            )
            return await self._failed_result(
                context,
                RuntimeException(
                    "LangGraph execution timed out",
                    runtime_name=self.name,
                    workflow_id=workflow.workflow_id,
                    execution_id=execution.execution_id,
                    attempt=context.attempt,
                    retryable=True,
                    timeout_seconds=context.timeout_seconds,
                    metadata={"kind": "timeout"},
                ),
                status="timeout",
            )
        except asyncio.CancelledError:
            return await self._cancelled_result(context)
        except RuntimeException as exc:
            return await self._failed_result(context, exc)
        except Exception as exc:  # pragma: no cover - defensive
            return await self._failed_result(
                context,
                RuntimeException(
                    "LangGraph execution failed",
                    runtime_name=self.name,
                    workflow_id=workflow.workflow_id,
                    execution_id=execution.execution_id,
                    attempt=context.attempt,
                    retryable=False,
                    metadata={"exception_type": type(exc).__name__},
                ),
            )

    async def _emit(self, context: RuntimeContext, event: str, payload: Mapping[str, Any]) -> None:
        emitter = context.event_emitter
        if emitter is None:
            return
        try:
            await emitter.emit(event, payload, context)
        except Exception:
            return

    def _context_to_state(self, context: RuntimeContext) -> dict[str, Any]:
        """Translate RuntimeContext into a LangGraph state payload."""
        return {
            "workflow_id": context.workflow.workflow_id,
            "workflow_name": context.workflow.name,
            "execution_id": context.execution.execution_id,
            "correlation_id": context.correlation_id,
            "tenant_id": context.tenant_id,
            "actor_id": context.actor_id,
            "attempt": context.attempt,
            "deadline": context.deadline.isoformat() if context.deadline is not None else None,
            "metadata": dict(context.metadata),
        }

    def _context_to_config(self, context: RuntimeContext) -> dict[str, Any]:
        """Translate RuntimeContext into a LangGraph config payload."""
        return {
            "runtime": {"name": self.name, "version": self.version},
            "execution": {
                "execution_id": context.execution.execution_id,
                "attempt": context.attempt,
                "max_attempts": context.max_attempts,
            },
            "correlation": {
                "correlation_id": context.correlation_id,
                "workflow_id": context.workflow.workflow_id,
                "execution_id": context.execution.execution_id,
            },
        }

    def _langgraph_output_to_result(self, output: Any) -> Any:
        """Normalize LangGraph output payload into a runtime-neutral result output."""
        return output

    def _with_attempt(self, context: RuntimeContext, attempt: int) -> RuntimeContext:
        return RuntimeContext(
            workflow=context.workflow,
            execution=context.execution,
            correlation_id=context.correlation_id,
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            deadline=context.deadline,
            timeout_seconds=context.timeout_seconds,
            attempt=attempt,
            max_attempts=context.max_attempts,
            retry_delay_seconds=context.retry_delay_seconds,
            cancellation_token=context.cancellation_token,
            event_emitter=context.event_emitter,
            metadata=dict(context.metadata),
        )

    async def _cancelled_result(self, context: RuntimeContext) -> RuntimeResult:
        await self._emit(
            context,
            "runtime.langgraph.cancelled",
            {"execution_id": context.execution.execution_id},
        )
        return RuntimeResult(
            workflow=context.workflow,
            execution=context.execution,
            status="cancelled",
            error=RuntimeException(
                "Execution cancelled",
                runtime_name=self.name,
                workflow_id=context.workflow.workflow_id,
                execution_id=context.execution.execution_id,
                attempt=context.attempt,
                retryable=False,
                metadata={"kind": "cancelled"},
            ),
            retryable=False,
            emitted_events=("runtime.langgraph.cancelled",),
        )

    async def _timeout_result(self, context: RuntimeContext) -> RuntimeResult:
        await self._emit(
            context,
            "runtime.langgraph.timeout",
            {"execution_id": context.execution.execution_id},
        )
        return RuntimeResult(
            workflow=context.workflow,
            execution=context.execution,
            status="timeout",
            error=RuntimeException(
                "Execution timed out",
                runtime_name=self.name,
                workflow_id=context.workflow.workflow_id,
                execution_id=context.execution.execution_id,
                attempt=context.attempt,
                retryable=True,
                timeout_seconds=context.timeout_seconds,
                metadata={"kind": "timeout"},
            ),
            retryable=True,
            retry_after_seconds=context.retry_delay_seconds,
            emitted_events=("runtime.langgraph.timeout",),
        )

    async def _failed_result(
        self,
        context: RuntimeContext,
        error: RuntimeException,
        *,
        status: str = "failed",
    ) -> RuntimeResult:
        await self._emit(
            context,
            "runtime.langgraph.failed",
            {
                "execution_id": context.execution.execution_id,
                "retryable": error.retryable,
                "attempt": context.attempt,
            },
        )
        return RuntimeResult(
            workflow=context.workflow,
            execution=context.execution,
            status=status,
            error=error,
            retryable=error.retryable,
            retry_after_seconds=context.retry_delay_seconds if error.retryable else None,
            emitted_events=("runtime.langgraph.failed",),
        )


def _duration_ms(started_at: datetime, ended_at: datetime) -> float:
    return max((ended_at - started_at).total_seconds() * 1000.0, 0.0)
