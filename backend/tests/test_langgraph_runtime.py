"""LangGraph runtime adapter tests.

These tests validate the adapter behavior without requiring LangGraph as a dependency.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from backend.runtime import RuntimeContext, RuntimeException, WorkflowExecution, WorkflowMetadata
from backend.runtime.langgraph import LangGraphRuntime


class FakeEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, Any]]] = []

    async def emit(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: RuntimeContext,
    ) -> None:
        self.events.append((event_name, payload))


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failures: list[Exception] = []
        self.sleep_seconds: float | None = None

    async def ainvoke(
        self,
        app: Any,
        *,
        state: Mapping[str, Any],
        config: Mapping[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"app": app, "state": dict(state), "config": dict(config or {})})
        if self.sleep_seconds is not None:
            await asyncio.sleep(self.sleep_seconds)
        if self.failures:
            raise self.failures.pop(0)
        return {"ok": True, "state": dict(state)}


def _context(
    *,
    app: Any,
    emitter: FakeEmitter | None = None,
    engine: FakeEngine | None = None,
    timeout_seconds: float | None = None,
    max_attempts: int = 1,
    retry_delay_seconds: float | None = None,
) -> tuple[LangGraphRuntime, RuntimeContext]:
    workflow = WorkflowMetadata(
        workflow_id="wf-1",
        name="wf",
        metadata={"langgraph_app": app},
    )
    execution = WorkflowExecution(execution_id="ex-1", workflow_id="wf-1", runtime_name="langgraph")
    deadline = None
    if timeout_seconds is None:
        deadline = datetime.now(UTC) + timedelta(seconds=5)
    else:
        deadline = datetime.now(UTC) + timedelta(seconds=float(timeout_seconds))
    context = RuntimeContext(
        workflow=workflow,
        execution=execution,
        correlation_id="corr-1",
        deadline=deadline,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        retry_delay_seconds=retry_delay_seconds,
        event_emitter=emitter,
    )
    runtime = LangGraphRuntime(engine=engine or FakeEngine())
    return runtime, context


@pytest.mark.asyncio
async def test_execute_translates_context_and_emits_events() -> None:
    emitter = FakeEmitter()
    engine = FakeEngine()
    runtime, context = _context(app=object(), emitter=emitter, engine=engine)

    result = await runtime.execute(context)

    assert result.status == "completed"
    assert engine.calls[0]["state"]["workflow_id"] == "wf-1"
    assert engine.calls[0]["state"]["execution_id"] == "ex-1"
    assert emitter.events[0][0].startswith("runtime.langgraph.")


@pytest.mark.asyncio
async def test_timeout_returns_timeout_result() -> None:
    engine = FakeEngine()
    engine.sleep_seconds = 0.2
    runtime, context = _context(app=object(), engine=engine, timeout_seconds=0.05)

    result = await runtime.execute(context)

    assert result.status == "timeout"
    assert result.error is not None
    assert result.retryable is True


@pytest.mark.asyncio
async def test_cancellation_returns_cancelled_result() -> None:
    engine = FakeEngine()
    engine.sleep_seconds = 1.0
    runtime, context = _context(app=object(), engine=engine)

    task = asyncio.create_task(runtime.execute(context))
    await asyncio.sleep(0.05)
    await runtime.cancel(context)
    result = await task

    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_retries_retryable_failures() -> None:
    engine = FakeEngine()
    engine.failures.append(RuntimeException("transient", retryable=True))
    runtime, context = _context(
        app=object(),
        engine=engine,
        max_attempts=2,
        retry_delay_seconds=0.0,
    )

    result = await runtime.execute(context)

    assert result.status == "completed"
    assert len(engine.calls) == 2


@pytest.mark.asyncio
async def test_missing_langgraph_app_returns_failed_result() -> None:
    workflow = WorkflowMetadata(workflow_id="wf-2", name="wf", metadata={})
    execution = WorkflowExecution(execution_id="ex-2", workflow_id="wf-2", runtime_name="langgraph")
    runtime = LangGraphRuntime(engine=FakeEngine())
    context = RuntimeContext(workflow=workflow, execution=execution)

    result = await runtime.execute(context)

    assert result.status == "failed"
    assert result.error is not None
