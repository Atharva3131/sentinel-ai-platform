"""Agent framework unit tests."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from backend.agents import (
    Agent,
    AgentContext,
    AgentException,
    AgentFactory,
    AgentInput,
    AgentLifecycle,
    AgentMetadata,
    AgentOutput,
    AgentRegistry,
    AgentStatus,
)
from backend.agents.executor import AgentRunner
from backend.agents.middleware import (
    AgentEvaluationMiddleware,
    AgentMiddlewarePipeline,
    ContextInjectionMiddleware,
    MetricsMiddleware,
    RequestValidationMiddleware,
)
from backend.agents.middleware.contracts import AgentExecutionRequest


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
    """Agent event emitter test double."""

    events: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)

    async def emit(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: AgentContext,
    ) -> None:
        self.events.append((event_name, dict(payload)))


@dataclass(slots=True)
class FakeMetrics:
    calls: list[dict[str, Any]]

    async def record_agent_execution(
        self,
        *,
        agent_name: str,
        agent_version: str | None,
        workflow_id: str | None,
        execution_id: str | None,
        status: str,
        duration_ms: float,
        attempt: int,
        correlation_id: str | None,
    ) -> None:
        self.calls.append(
            {
                "agent_name": agent_name,
                "agent_version": agent_version,
                "workflow_id": workflow_id,
                "execution_id": execution_id,
                "status": status,
                "duration_ms": duration_ms,
                "attempt": attempt,
                "correlation_id": correlation_id,
            }
        )


@dataclass(slots=True)
class FakeEvaluation:
    calls: list[tuple[str, Any]]

    async def before(self, context: AgentContext) -> None:
        self.calls.append(("before", context.attempt))

    async def after(self, context: AgentContext, *, status: str) -> None:
        self.calls.append(("after", status))


@dataclass(slots=True)
class DeterministicAgent(Agent):
    """Agent that echoes injected metadata."""

    _metadata: AgentMetadata

    @property
    def metadata(self) -> AgentMetadata:
        return self._metadata

    async def execute(self, context: AgentContext, agent_input: AgentInput) -> AgentOutput:
        return AgentOutput(payload={"attempt": context.attempt, **dict(context.metadata)})


@dataclass(slots=True)
class RetryThenSucceedAgent(Agent):
    _metadata: AgentMetadata

    @property
    def metadata(self) -> AgentMetadata:
        return self._metadata

    async def execute(self, context: AgentContext, agent_input: AgentInput) -> AgentOutput:
        if context.attempt == 1:
            raise AgentException(
                "temporary",
                agent_name=self.name,
                agent_version=self.version,
                workflow_id=context.workflow_id,
                execution_id=context.execution_id,
                attempt=context.attempt,
                retryable=True,
                metadata={"kind": "temporary"},
            )
        return AgentOutput(payload={"ok": True, "attempt": context.attempt})


@dataclass(slots=True)
class BlockingAgent(Agent):
    _metadata: AgentMetadata
    started: asyncio.Event

    @property
    def metadata(self) -> AgentMetadata:
        return self._metadata

    async def execute(self, context: AgentContext, agent_input: AgentInput) -> AgentOutput:
        self.started.set()
        await asyncio.sleep(3600)
        return AgentOutput(payload={"ok": True})


def _registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(
        name="alpha",
        version="1.0.0",
        provider=lambda _: DeterministicAgent(AgentMetadata(name="alpha", version="1.0.0")),
        default=True,
    )
    registry.register(
        name="alpha",
        version="1.1.0",
        provider=lambda _: DeterministicAgent(AgentMetadata(name="alpha", version="1.1.0")),
    )
    return registry


def _context(
    *,
    cancellation_token: FakeCancellationToken | None = None,
    emitter: FakeEmitter | None = None,
    metrics: FakeMetrics | None = None,
    evaluation: FakeEvaluation | None = None,
    max_attempts: int = 1,
    timeout_seconds: float | None = None,
) -> AgentContext:
    return AgentContext(
        workflow_id="wf-1",
        execution_id="ex-1",
        correlation_id="corr-1",
        attempt=1,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        cancellation_token=cancellation_token,
        event_emitter=emitter,
        metrics_recorder=metrics,
        evaluation_hook=evaluation,
        metadata={"seed": "value"},
    )


def test_registry_resolves_highest_version_by_default() -> None:
    registry = _registry()
    version, _ = registry.resolve("alpha")
    assert version == "1.1.0"
    assert registry.versions("alpha") == ("1.0.0", "1.1.0")


def test_factory_constructs_new_agent_instances() -> None:
    factory = AgentFactory(_registry())
    a1 = factory.create("alpha")
    a2 = factory.create("alpha")
    assert a1 is not a2


@pytest.mark.asyncio
async def test_runner_executes_agent_and_emits_events() -> None:
    emitter = FakeEmitter()
    metrics = FakeMetrics(calls=[])

    class Injector:
        def inject(self, request: AgentExecutionRequest) -> Mapping[str, Any]:
            return {"injected": "yes"}

    pipeline = AgentMiddlewarePipeline(
        middlewares=(
            ContextInjectionMiddleware(
                injectors=(Injector(),),
            ),
            MetricsMiddleware(),
        )
    )
    factory = AgentFactory(_registry())
    runner = AgentRunner(factory=factory, middleware=pipeline, lifecycle=AgentLifecycle())
    result = await runner.execute(
        "alpha",
        context=_context(emitter=emitter, metrics=metrics),
        agent_input=AgentInput(),
    )

    assert result.status == AgentStatus.COMPLETED
    assert result.output is not None
    assert result.output.payload["seed"] == "value"
    assert result.output.payload["injected"] == "yes"
    assert emitter.events[0][0] == "agent.started"
    assert emitter.events[-1][0] == "agent.completed"
    assert metrics.calls


@pytest.mark.asyncio
async def test_runner_retries_retryable_failures() -> None:
    registry = AgentRegistry()
    registry.register(
        name="beta",
        version="1.0.0",
        provider=lambda _: RetryThenSucceedAgent(AgentMetadata(name="beta", version="1.0.0")),
        default=True,
    )
    emitter = FakeEmitter()
    runner = AgentRunner(factory=AgentFactory(registry))
    result = await runner.execute(
        "beta",
        context=_context(emitter=emitter, max_attempts=2, timeout_seconds=None),
        agent_input=AgentInput(),
    )

    assert result.status == AgentStatus.COMPLETED
    assert any(event[0] == "agent.retried" for event in emitter.events)
    assert any(event[0] == "agent.completed" for event in emitter.events)


@pytest.mark.asyncio
async def test_runner_cancels_in_flight_agent_execution() -> None:
    started = asyncio.Event()
    registry = AgentRegistry()
    registry.register(
        name="gamma",
        version="1.0.0",
        provider=lambda _: BlockingAgent(
            AgentMetadata(name="gamma", version="1.0.0"),
            started=started,
        ),
        default=True,
    )
    token = FakeCancellationToken()
    emitter = FakeEmitter()
    runner = AgentRunner(factory=AgentFactory(registry))

    task = asyncio.create_task(
        runner.execute(
            "gamma",
            context=_context(cancellation_token=token, emitter=emitter, timeout_seconds=None),
            agent_input=AgentInput(),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    token.cancel()
    result = await asyncio.wait_for(task, timeout=1)

    assert result.status == AgentStatus.CANCELLED
    assert any(event[0] == "agent.cancelled" for event in emitter.events)


@pytest.mark.asyncio
async def test_runner_times_out_execution() -> None:
    started = asyncio.Event()
    registry = AgentRegistry()
    registry.register(
        name="delta",
        version="1.0.0",
        provider=lambda _: BlockingAgent(
            AgentMetadata(name="delta", version="1.0.0"),
            started=started,
        ),
        default=True,
    )
    emitter = FakeEmitter()
    runner = AgentRunner(factory=AgentFactory(registry))
    result = await runner.execute(
        "delta",
        context=_context(emitter=emitter, max_attempts=1, timeout_seconds=0.01),
        agent_input=AgentInput(),
    )

    assert result.status == AgentStatus.TIMED_OUT
    assert any(event[0] == "agent.timed_out" for event in emitter.events)


@pytest.mark.asyncio
async def test_evaluation_middleware_invokes_hooks() -> None:
    evaluation = FakeEvaluation(calls=[])
    pipeline = AgentMiddlewarePipeline(middlewares=(AgentEvaluationMiddleware(hook=evaluation),))
    runner = AgentRunner(factory=AgentFactory(_registry()), middleware=pipeline)
    result = await runner.execute(
        "alpha",
        context=_context(evaluation=evaluation),
        agent_input=AgentInput(),
    )

    assert result.status == AgentStatus.COMPLETED
    assert evaluation.calls[0] == ("before", 1)
    assert evaluation.calls[-1] == ("after", "completed")


@pytest.mark.asyncio
async def test_request_validation_middleware_rejects() -> None:
    class RejectingValidator:
        def validate(self, request: AgentExecutionRequest) -> None:
            raise AgentException("invalid", agent_name=request.agent.name)

    registry = _registry()
    runner = AgentRunner(
        factory=AgentFactory(registry),
        middleware=AgentMiddlewarePipeline(middlewares=(RequestValidationMiddleware(RejectingValidator()),)),
    )

    result = await runner.execute(
        "alpha",
        context=_context(),
        agent_input=AgentInput(),
    )
    assert result.status == AgentStatus.FAILED
    assert result.error is not None
    assert "invalid" in str(result.error)
