"""Workflow engine unit tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from backend.runtime import RuntimeContext, RuntimeException, RuntimeResult
from backend.workflows import (
    ExecutionPolicy,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowLifecycle,
    WorkflowRegistry,
    WorkflowState,
    WorkflowValidator,
)
from backend.workflows.validator import WorkflowValidationError


class FakeCancellationToken:
    """Cooperative cancellation double."""

    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def is_set(self) -> bool:
        return self.cancelled

    async def wait(self) -> None:
        return None


class FakeEmitter:
    """Workflow event emitter double."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, Any]]] = []

    async def emit(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: WorkflowContext,
    ) -> None:
        self.events.append((event_name, payload))


class FakeRuntime:
    """Runtime double that replays a configured sequence of results."""

    def __init__(self, name: str, responses: list[RuntimeResult]) -> None:
        self._name = name
        self._responses = responses
        self.contexts: list[RuntimeContext] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str | None:
        return "1.0.0"

    @property
    def capabilities(self) -> tuple[str, ...]:
        return ("generic",)

    def supports(self, workflow: WorkflowDefinition) -> bool:
        return True

    async def execute(self, context: RuntimeContext) -> RuntimeResult:
        self.contexts.append(context)
        if context.event_emitter is not None:
            await context.event_emitter.emit(
                "runtime.invoked",
                {"runtime": self._name},
                context,
            )
        if not self._responses:
            raise RuntimeError("no more responses")
        return self._responses.pop(0)

    async def cancel(self, context: RuntimeContext) -> None:
        return None


def _definition_builder() -> WorkflowBuilder:
    return (
        WorkflowBuilder(workflow_id="wf-1", name="incident", version="1.0.0")
        .with_runtime("fake")
        .with_capabilities("generic")
        .with_metadata(source="unit-test")
        .with_execution_policy(
            ExecutionPolicy(
                max_attempts=2,
                base_retry_delay_seconds=0.0,
                checkpoint_required=True,
            )
        )
        .with_lifecycle(WorkflowLifecycle())
    )


def _runtime_factory(runtime: FakeRuntime) -> Any:
    class _Factory:
        def resolve(self, name: str | None = None) -> FakeRuntime:
            return runtime

    return _Factory()


def _result(
    *,
    status: str,
    checkpoint_id: str | None = None,
    retryable: bool = False,
    retry_after_seconds: float | None = None,
    error: RuntimeException | None = None,
) -> RuntimeResult:
    from backend.runtime import WorkflowExecution, WorkflowMetadata

    workflow = WorkflowMetadata(
        workflow_id="wf-1",
        name="incident",
        version="1.0.0",
        capabilities=("generic",),
    )
    execution = WorkflowExecution(
        execution_id="ex-1",
        workflow_id="wf-1",
        runtime_name="fake",
        checkpoint_id=None,
        started_at=datetime.now(UTC),
    )
    return RuntimeResult(
        workflow=workflow,
        execution=execution,
        status=status,
        error=error,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
        metadata={} if checkpoint_id is None else {"checkpoint_id": checkpoint_id},
    )


@pytest.mark.asyncio
async def test_builder_and_registry_support_versioned_definitions() -> None:
    definition_v1 = _definition_builder().build()
    definition_v2 = _definition_builder().with_version("1.1.0").build()
    registry = WorkflowRegistry()
    registry.register(definition_v1)
    registry.register(definition_v2)

    assert registry.resolve("incident").version == "1.1.0"
    assert registry.resolve("wf-1", "1.0.0").version == "1.0.0"
    assert registry.versions("incident") == ("1.0.0", "1.1.0")


@pytest.mark.asyncio
async def test_executor_runs_async_workflow_and_emits_events() -> None:
    emitter = FakeEmitter()
    runtime = FakeRuntime("fake", [_result(status="completed", checkpoint_id="ckpt-1")])
    registry = WorkflowRegistry()
    definition = _definition_builder().build()
    registry.register(definition, default=True)
    executor = WorkflowExecutor(registry, runtime_factory=_runtime_factory(runtime))
    context = WorkflowContext(
        workflow_id="wf-1",
        workflow_version="1.0.0",
        execution_id="ex-1",
        runtime_name="fake",
        event_emitter=emitter,
        metadata={"source": "unit-test"},
    )

    state = await executor.execute(context)

    assert state.status == "completed"
    assert state.checkpoint_id == "ckpt-1"
    assert runtime.contexts[0].execution.checkpoint_id is None
    assert any(event[0] == "runtime.invoked" for event in emitter.events)
    assert emitter.events[0][0] == "workflow.started"
    assert emitter.events[-1][0] == "workflow.completed"


@pytest.mark.asyncio
async def test_executor_retries_retryable_failures_and_uses_checkpoint() -> None:
    emitter = FakeEmitter()
    runtime = FakeRuntime(
        "fake",
        [
            _result(
                status="failed",
                checkpoint_id="ckpt-1",
                retryable=True,
                error=RuntimeException("temporary", retryable=True),
            ),
            _result(status="completed", checkpoint_id="ckpt-2"),
        ],
    )
    registry = WorkflowRegistry()
    definition = _definition_builder().build()
    registry.register(definition, default=True)
    executor = WorkflowExecutor(registry, runtime_factory=_runtime_factory(runtime))
    context = WorkflowContext(
        workflow_id="wf-1",
        workflow_version="1.0.0",
        execution_id="ex-1",
        runtime_name="fake",
        event_emitter=emitter,
        max_attempts=2,
    )

    state = await executor.execute(context)

    assert state.status == "completed"
    assert state.checkpoint_id == "ckpt-2"
    assert len(runtime.contexts) == 2
    assert runtime.contexts[1].execution.checkpoint_id == "ckpt-1"
    assert any(event[0] == "workflow.retry_scheduled" for event in emitter.events)


@pytest.mark.asyncio
async def test_executor_cancels_before_runtime_when_requested() -> None:
    runtime = FakeRuntime("fake", [_result(status="completed")])
    registry = WorkflowRegistry()
    definition = _definition_builder().build()
    registry.register(definition, default=True)
    executor = WorkflowExecutor(registry, runtime_factory=_runtime_factory(runtime))
    context = WorkflowContext(
        workflow_id="wf-1",
        workflow_version="1.0.0",
        execution_id="ex-1",
        runtime_name="fake",
        cancellation_token=FakeCancellationToken(cancelled=True),
    )

    state = await executor.execute(context)

    assert state.status == "cancelled"
    assert runtime.contexts == []


@pytest.mark.asyncio
async def test_executor_recovers_from_checkpointed_state() -> None:
    emitter = FakeEmitter()
    runtime = FakeRuntime("fake", [_result(status="completed", checkpoint_id="ckpt-2")])
    registry = WorkflowRegistry()
    definition = _definition_builder().build()
    registry.register(definition, default=True)
    executor = WorkflowExecutor(registry, runtime_factory=_runtime_factory(runtime))
    state = WorkflowState.initial(
        workflow_id="wf-1",
        workflow_version="1.0.0",
        execution_id="ex-1",
    ).with_status("paused", checkpoint_id="ckpt-1")
    context = WorkflowContext(
        workflow_id="wf-1",
        workflow_version="1.0.0",
        execution_id="ex-1",
        runtime_name="fake",
        event_emitter=emitter,
    )

    recovered = await executor.recover(context, state)

    assert recovered.status == "completed"
    assert recovered.recovery_count == 1
    assert runtime.contexts[0].execution.checkpoint_id == "ckpt-1"
    assert any(event[0] == "workflow.recovery.started" for event in emitter.events)


def test_validator_rejects_invalid_definition() -> None:
    validator = WorkflowValidator()
    with pytest.raises(WorkflowValidationError):
        validator.validate_definition(
            WorkflowDefinition(
                workflow_id="",
                name="incident",
                version="1.0.0",
            )
        )
