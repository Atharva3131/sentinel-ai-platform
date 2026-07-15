"""Runtime core contract tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from backend.runtime import (
    RuntimeContext,
    RuntimeException,
    RuntimeFactory,
    RuntimeRegistry,
    RuntimeResult,
    WorkflowExecution,
    WorkflowMetadata,
)


class FakeCancellationToken:
    """Cooperative cancellation double."""

    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def is_set(self) -> bool:
        return self.cancelled

    async def wait(self) -> None:
        return None


class FakeEventEmitter:
    """Runtime event emission double."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, Any], RuntimeContext]] = []

    async def emit(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: RuntimeContext,
    ) -> None:
        self.events.append((event_name, payload, context))


class FakeRuntime:
    """Runtime double used to validate registry and factory behavior."""

    def __init__(self, name: str, capability: str) -> None:
        self._name = name
        self._capability = capability
        self.executions: list[RuntimeContext] = []
        self.cancellations: list[RuntimeContext] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str | None:
        return "1.0.0"

    @property
    def capabilities(self) -> tuple[str, ...]:
        return (self._capability,)

    def supports(self, workflow: WorkflowMetadata) -> bool:
        return self._capability in workflow.capabilities

    async def execute(self, context: RuntimeContext) -> RuntimeResult:
        self.executions.append(context)
        assert context.event_emitter is not None
        await context.event_emitter.emit(
            "runtime.executed",
            {"runtime": self.name},
            context,
        )
        return RuntimeResult(
            workflow=context.workflow,
            execution=context.execution,
            status="completed",
            output={"execution_id": context.execution.execution_id},
            emitted_events=("runtime.executed",),
        )

    async def cancel(self, context: RuntimeContext) -> None:
        self.cancellations.append(context)


@pytest.mark.asyncio
async def test_registry_and_factory_resolve_multiple_runtimes() -> None:
    registry: RuntimeRegistry = RuntimeRegistry()
    read_runtime = FakeRuntime("read", "read-only")
    write_runtime = FakeRuntime("write", "write-enabled")
    registry.register("read", read_runtime, default=True)
    registry.register("write", write_runtime)
    factory = RuntimeFactory(registry)

    assert factory.resolve().name == "read"
    assert factory.resolve("write").name == "write"
    assert registry.names() == ("read", "write")


@pytest.mark.asyncio
async def test_runtime_context_supports_timeout_and_cancellation() -> None:
    workflow = WorkflowMetadata(
        workflow_id="wf-1",
        name="incident-workflow",
        capabilities=("read-only",),
    )
    execution = WorkflowExecution(
        execution_id="ex-1",
        workflow_id="wf-1",
        runtime_name="read",
        started_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    context = RuntimeContext(
        workflow=workflow,
        execution=execution,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        timeout_seconds=30.0,
        cancellation_token=FakeCancellationToken(),
    )

    assert context.is_cancelled() is False
    assert context.has_timed_out() is False
    assert context.remaining_timeout_seconds() is not None
    assert execution.duration_seconds() is not None


@pytest.mark.asyncio
async def test_runtime_execution_can_emit_events_and_return_result() -> None:
    registry: RuntimeRegistry = RuntimeRegistry()
    runtime = FakeRuntime("read", "read-only")
    registry.register("default", runtime, default=True)
    factory = RuntimeFactory(registry)

    workflow = factory.create_workflow_metadata(
        workflow_id="wf-2",
        name="diagnostic-workflow",
        capabilities=("read-only",),
    )
    execution = factory.create_execution(
        execution_id="ex-2",
        workflow_id="wf-2",
        runtime_name="read",
    )
    emitter = FakeEventEmitter()
    context = factory.create_context(
        workflow=workflow,
        execution=execution,
        correlation_id="corr-1",
        event_emitter=emitter,
    )

    result = await runtime.execute(context)
    await runtime.cancel(context)

    assert result.status == "completed"
    assert result.execution.execution_id == "ex-2"
    assert emitter.events[0][0] == "runtime.executed"
    assert runtime.executions[0].correlation_id == "corr-1"
    assert runtime.cancellations[0].execution.execution_id == "ex-2"


def test_runtime_exception_carries_execution_metadata() -> None:
    error = RuntimeException(
        "runtime failed",
        runtime_name="read",
        workflow_id="wf-3",
        execution_id="ex-3",
        attempt=2,
        retryable=True,
        timeout_seconds=5.0,
        metadata={"reason": "timeout"},
    )

    assert error.runtime_name == "read"
    assert error.workflow_id == "wf-3"
    assert error.execution_id == "ex-3"
    assert error.retryable is True
    assert error.metadata["reason"] == "timeout"
