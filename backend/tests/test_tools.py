"""Tool framework unit tests."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from backend.tools import (
    Tool,
    ToolContext,
    ToolException,
    ToolExecutor,
    ToolInput,
    ToolMetadata,
    ToolPermission,
    ToolRegistry,
)
from backend.tools.middleware import (
    LoggingMiddleware,
    MetricsMiddleware,
    OpenTelemetryMiddleware,
    PermissionMiddleware,
    RequestValidationMiddleware,
    SchemaValidationMiddleware,
    TimingMiddleware,
    ToolMiddlewarePipeline,
)
from backend.tools.validator import ToolValidator


class FakeCancellationToken:
    """Cooperative cancellation token test double."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


@dataclass(slots=True)
class FakeEmitter:
    events: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)

    async def emit(self, event_name: str, payload: Mapping[str, Any], context: ToolContext) -> None:
        self.events.append((event_name, dict(payload)))


@dataclass(slots=True)
class FakeMetrics:
    calls: list[dict[str, Any]]

    async def record_tool_execution(
        self,
        *,
        tool_name: str,
        tool_version: str | None,
        workflow_id: str | None,
        execution_id: str | None,
        status: str,
        duration_ms: float,
        attempt: int,
        correlation_id: str | None,
    ) -> None:
        self.calls.append(
            {
                "tool_name": tool_name,
                "tool_version": tool_version,
                "workflow_id": workflow_id,
                "execution_id": execution_id,
                "status": status,
                "duration_ms": duration_ms,
                "attempt": attempt,
                "correlation_id": correlation_id,
            }
        )


@dataclass(slots=True)
class DeterministicTool(Tool):
    _metadata: ToolMetadata

    @property
    def metadata(self) -> ToolMetadata:
        return self._metadata

    async def execute(self, context: ToolContext, tool_input: ToolInput) -> object:
        return {"attempt": context.attempt, **dict(context.metadata), **dict(tool_input.payload)}


@dataclass(slots=True)
class RetryThenSucceedTool(Tool):
    _metadata: ToolMetadata

    @property
    def metadata(self) -> ToolMetadata:
        return self._metadata

    async def execute(self, context: ToolContext, tool_input: ToolInput) -> object:
        if context.attempt == 1:
            raise ToolException(
                "temporary",
                tool_name=self.name,
                tool_version=self.version,
                workflow_id=context.workflow_id,
                execution_id=context.execution_id,
                attempt=context.attempt,
                retryable=True,
                metadata={"kind": "temporary"},
            )
        return {"ok": True, "attempt": context.attempt}


@dataclass(slots=True)
class BlockingTool(Tool):
    _metadata: ToolMetadata
    started: asyncio.Event

    @property
    def metadata(self) -> ToolMetadata:
        return self._metadata

    async def execute(self, context: ToolContext, tool_input: ToolInput) -> object:
        self.started.set()
        await asyncio.sleep(3600)
        return {"ok": True}


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        name="alpha",
        version="1.0.0",
        provider=lambda _: DeterministicTool(ToolMetadata(name="alpha", version="1.0.0")),
        default=True,
    )
    registry.register(
        name="alpha",
        version="1.1.0",
        provider=lambda _: DeterministicTool(ToolMetadata(name="alpha", version="1.1.0")),
    )
    return registry


def _context(
    *,
    permissions: tuple[ToolPermission, ...] = (),
    cancellation_token: FakeCancellationToken | None = None,
    emitter: FakeEmitter | None = None,
    metrics: FakeMetrics | None = None,
    max_attempts: int = 1,
    timeout_seconds: float | None = None,
) -> ToolContext:
    ctx = ToolContext(
        workflow_id="wf-1",
        execution_id="ex-1",
        correlation_id="corr-1",
        attempt=1,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        cancellation_token=cancellation_token,
        event_emitter=emitter,
        metrics_recorder=metrics,
        metadata={"seed": "value"},
    )
    if permissions:
        ctx = ctx.with_permissions(*permissions)
    return ctx


def test_registry_resolves_highest_version_by_default() -> None:
    registry = _registry()
    version, _ = registry.resolve("alpha")
    assert version == "1.1.0"
    assert registry.versions("alpha") == ("1.0.0", "1.1.0")


@pytest.mark.asyncio
async def test_executor_runs_tool_with_middleware_and_emits_events() -> None:
    emitter = FakeEmitter()
    metrics = FakeMetrics(calls=[])
    validator = ToolValidator()
    pipeline = ToolMiddlewarePipeline(
        middlewares=(
            RequestValidationMiddleware(),
            PermissionMiddleware(validator),
            SchemaValidationMiddleware(validator),
            LoggingMiddleware(),
            OpenTelemetryMiddleware(),
            MetricsMiddleware(),
            TimingMiddleware(),
        )
    )
    registry = _registry()
    executor = ToolExecutor(registry=registry, validator=validator, middleware=pipeline)
    tool_input = ToolInput(payload={"x": 1})

    result = await executor.execute(
        "alpha",
        context=_context(
            emitter=emitter,
            metrics=metrics,
            permissions=(ToolPermission.READ,),
        ),
        tool_input=tool_input,
    )

    assert result.status == "completed"
    assert result.output["seed"] == "value"
    assert result.output["x"] == 1
    assert emitter.events[0][0] == "tool.started"
    assert emitter.events[-1][0] == "tool.completed"
    assert metrics.calls


@pytest.mark.asyncio
async def test_permission_middleware_denies_missing_permissions() -> None:
    registry = ToolRegistry()
    registry.register(
        name="secure",
        version="1.0.0",
        provider=lambda _: DeterministicTool(
            ToolMetadata(name="secure", version="1.0.0", permissions=(ToolPermission.WRITE,))
        ),
        default=True,
    )
    validator = ToolValidator()
    pipeline = ToolMiddlewarePipeline(middlewares=(PermissionMiddleware(validator),))
    executor = ToolExecutor(registry=registry, validator=validator, middleware=pipeline)

    result = await executor.execute(
        "secure",
        context=_context(permissions=(ToolPermission.READ,)),
        tool_input=ToolInput(payload={}),
    )

    assert result.status == "failed"
    assert result.error is not None
    assert "permission" in str(result.error).lower()
    assert "missing_permissions" in result.metadata


@pytest.mark.asyncio
async def test_schema_validation_rejects_invalid_input() -> None:
    registry = ToolRegistry()
    registry.register(
        name="schema",
        version="1.0.0",
        provider=lambda _: DeterministicTool(
            ToolMetadata(
                name="schema",
                version="1.0.0",
                input_schema={
                    "type": "object",
                    "required": ["x"],
                    "properties": {"x": {"type": "integer"}},
                },
            )
        ),
        default=True,
    )
    validator = ToolValidator()
    pipeline = ToolMiddlewarePipeline(middlewares=(SchemaValidationMiddleware(validator),))
    executor = ToolExecutor(registry=registry, validator=validator, middleware=pipeline)

    result = await executor.execute(
        "schema",
        context=_context(),
        tool_input=ToolInput(payload={}),
    )

    assert result.status == "failed"
    assert result.error is not None
    assert "missing" in str(result.error).lower()


@pytest.mark.asyncio
async def test_executor_retries_retryable_failures() -> None:
    registry = ToolRegistry()
    registry.register(
        name="beta",
        version="1.0.0",
        provider=lambda _: RetryThenSucceedTool(ToolMetadata(name="beta", version="1.0.0")),
        default=True,
    )
    emitter = FakeEmitter()
    executor = ToolExecutor(registry=registry)
    result = await executor.execute(
        "beta",
        context=_context(emitter=emitter, max_attempts=2),
        tool_input=ToolInput(payload={}),
    )

    assert result.status == "completed"
    assert any(event[0] == "tool.retried" for event in emitter.events)
    assert any(event[0] == "tool.completed" for event in emitter.events)


@pytest.mark.asyncio
async def test_executor_cancels_in_flight_tool_execution() -> None:
    started = asyncio.Event()
    registry = ToolRegistry()
    registry.register(
        name="gamma",
        version="1.0.0",
        provider=lambda _: BlockingTool(
            ToolMetadata(name="gamma", version="1.0.0"),
            started=started,
        ),
        default=True,
    )
    token = FakeCancellationToken()
    emitter = FakeEmitter()
    executor = ToolExecutor(registry=registry)

    task = asyncio.create_task(
        executor.execute(
            "gamma",
            context=_context(cancellation_token=token, emitter=emitter, timeout_seconds=None),
            tool_input=ToolInput(payload={}),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    token.cancel()
    result = await asyncio.wait_for(task, timeout=1)

    assert result.status == "cancelled"
    assert any(event[0] == "tool.cancelled" for event in emitter.events)


@pytest.mark.asyncio
async def test_executor_times_out_execution() -> None:
    started = asyncio.Event()
    registry = ToolRegistry()
    registry.register(
        name="delta",
        version="1.0.0",
        provider=lambda _: BlockingTool(
            ToolMetadata(name="delta", version="1.0.0"),
            started=started,
        ),
        default=True,
    )
    emitter = FakeEmitter()
    executor = ToolExecutor(registry=registry)
    result = await executor.execute(
        "delta",
        context=_context(emitter=emitter, max_attempts=1, timeout_seconds=0.01),
        tool_input=ToolInput(payload={}),
    )

    assert result.status == "timed_out"
    assert any(event[0] == "tool.timed_out" for event in emitter.events)
