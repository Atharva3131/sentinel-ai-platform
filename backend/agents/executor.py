"""Agent execution runner with retries, timeouts, and middleware."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backend.agents.agent import Agent
from backend.agents.context import AgentContext
from backend.agents.exceptions import AgentException
from backend.agents.factory import AgentFactory
from backend.agents.lifecycle import AgentLifecycle, AgentStatus
from backend.agents.middleware.contracts import AgentExecutionRequest
from backend.agents.middleware.pipeline import AgentMiddlewarePipeline
from backend.agents.models import AgentExecutionResult, AgentInput, AgentMetadata


@dataclass(slots=True)
class AgentRunner:
    """Execute agents with standardized cross-cutting concerns."""

    factory: AgentFactory
    middleware: AgentMiddlewarePipeline = field(default_factory=AgentMiddlewarePipeline)
    lifecycle: AgentLifecycle = field(default_factory=AgentLifecycle)

    async def execute(
        self,
        name: str,
        *,
        context: AgentContext,
        agent_input: AgentInput,
        version: str | None = None,
        dependencies: dict[str, Any] | None = None,
    ) -> AgentExecutionResult:
        """Execute an agent with retries and cancellation."""
        attempt = max(context.attempt, 1)
        max_attempts = max(context.max_attempts, 1)

        while attempt <= max_attempts:
            context_attempt = context.with_attempt(attempt)
            if context_attempt.is_cancelled():
                result = self._cancelled_result(
                    self._agent_metadata(name, version),
                    context_attempt,
                )
                await self._emit(context_attempt, "agent.cancelled", self._event_payload(result))
                return result

            agent = self.factory.create(name, version=version, dependencies=dependencies)
            await self._emit(
                context_attempt,
                "agent.started",
                self._started_payload(agent, context_attempt),
            )

            started_at = datetime.now(UTC)
            try:
                result = await self._execute_once(
                    agent,
                    context_attempt,
                    agent_input,
                    started_at=started_at,
                )
            except AgentException as exc:
                result = self._failed_result(
                    agent.metadata,
                    context_attempt,
                    exc,
                    started_at=started_at,
                )
            except Exception as exc:  # pragma: no cover - defensive
                result = self._failed_result(
                    agent.metadata,
                    context_attempt,
                    AgentException(
                        "Agent execution failed",
                        agent_name=agent.name,
                        agent_version=agent.version,
                        workflow_id=context_attempt.workflow_id,
                        execution_id=context_attempt.execution_id,
                        attempt=context_attempt.attempt,
                        retryable=False,
                        metadata={"exception_type": type(exc).__name__},
                    ),
                    started_at=started_at,
                )

            await self._emit(context_attempt, f"agent.{result.status}", self._event_payload(result))

            if self._should_retry(result, attempt, max_attempts):
                await self._emit(
                    context_attempt,
                    "agent.retried",
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

        final_agent = self._agent_metadata(name, version)
        exhausted = AgentException(
            "Agent retry budget exhausted",
            agent_name=name,
            agent_version=version,
            workflow_id=context.workflow_id,
            execution_id=context.execution_id,
            attempt=attempt,
            retryable=False,
        )
        result = self._failed_result(
            final_agent,
            context.with_attempt(attempt),
            exhausted,
            started_at=None,
        )
        await self._emit(context, "agent.failed", self._event_payload(result))
        return result

    async def _execute_once(
        self,
        agent: Agent,
        context: AgentContext,
        agent_input: AgentInput,
        *,
        started_at: datetime,
    ) -> AgentExecutionResult:
        timeout = context.remaining_timeout_seconds()

        async def terminal(request: AgentExecutionRequest) -> AgentExecutionResult:
            output = await agent.execute(request.context, request.agent_input)
            ended_at = datetime.now(UTC)
            return AgentExecutionResult(
                agent=agent.metadata,
                workflow_id=context.workflow_id,
                execution_id=context.execution_id,
                status=AgentStatus.COMPLETED,
                output=output,
                retryable=False,
                started_at=started_at,
                ended_at=ended_at,
                metadata=dict(context.metadata),
            )

        handler = self.middleware.wrap(terminal)
        request = AgentExecutionRequest(agent=agent, context=context, agent_input=agent_input)

        async def invoke() -> AgentExecutionResult:
            if timeout is None:
                return await handler(request)
            return await asyncio.wait_for(handler(request), timeout=timeout)

        run_task = asyncio.create_task(invoke(), name=f"agent:{agent.name}:{context.execution_id}")

        if context.cancellation_token is None:
            try:
                return await run_task
            except TimeoutError as exc:
                raise AgentException(
                    "Agent execution timed out",
                    agent_name=agent.name,
                    agent_version=agent.version,
                    workflow_id=context.workflow_id,
                    execution_id=context.execution_id,
                    attempt=context.attempt,
                    retryable=True,
                    timeout_seconds=timeout,
                    metadata={"kind": "timeout"},
                ) from exc

        cancel_task = asyncio.create_task(context.cancellation_token.wait(), name="agent:cancel")
        done, _pending = await asyncio.wait(
            {run_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done and not run_task.done():
            run_task.cancel()
            with contextlib.suppress(BaseException):
                await run_task
            raise AgentException(
                "Agent execution cancelled",
                agent_name=agent.name,
                agent_version=agent.version,
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
            raise AgentException(
                "Agent execution timed out",
                agent_name=agent.name,
                agent_version=agent.version,
                workflow_id=context.workflow_id,
                execution_id=context.execution_id,
                attempt=context.attempt,
                retryable=True,
                timeout_seconds=timeout,
                metadata={"kind": "timeout"},
            ) from exc

    def _should_retry(
        self,
        result: AgentExecutionResult,
        attempt: int,
        max_attempts: int,
    ) -> bool:
        if attempt >= max_attempts:
            return False
        return bool(result.retryable) and result.status in {
            AgentStatus.FAILED,
            AgentStatus.TIMED_OUT,
        }

    def _agent_metadata(self, name: str, version: str | None) -> AgentMetadata:
        return AgentMetadata(name=name, version=version)

    def _started_payload(self, agent: Agent, context: AgentContext) -> dict[str, Any]:
        return {
            "agent_name": agent.name,
            "agent_version": agent.version,
            "workflow_id": context.workflow_id,
            "execution_id": context.execution_id,
            "correlation_id": context.correlation_id,
            "attempt": context.attempt,
        }

    def _event_payload(self, result: AgentExecutionResult) -> dict[str, Any]:
        return {
            "agent_name": result.agent.name,
            "agent_version": result.agent.version,
            "workflow_id": result.workflow_id,
            "execution_id": result.execution_id,
            "status": str(result.status),
            "retryable": result.retryable,
            "metadata": dict(result.metadata),
        }

    def _cancelled_result(
        self,
        agent: AgentMetadata,
        context: AgentContext,
    ) -> AgentExecutionResult:
        ended_at = datetime.now(UTC)
        return AgentExecutionResult(
            agent=agent,
            workflow_id=context.workflow_id,
            execution_id=context.execution_id,
            status=AgentStatus.CANCELLED,
            error=AgentException(
                "Cancelled",
                agent_name=agent.name,
                agent_version=agent.version,
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
        agent: AgentMetadata,
        context: AgentContext,
        error: AgentException,
        *,
        started_at: datetime | None,
    ) -> AgentExecutionResult:
        ended_at = datetime.now(UTC)
        kind = error.metadata.get("kind")
        if kind == "timeout":
            status = AgentStatus.TIMED_OUT
        elif kind == "cancelled":
            status = AgentStatus.CANCELLED
        else:
            status = AgentStatus.FAILED
        return AgentExecutionResult(
            agent=agent,
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

    async def _emit(self, context: AgentContext, event_name: str, payload: dict[str, Any]) -> None:
        emitter = context.event_emitter
        if emitter is None:
            return
        try:
            await emitter.emit(event_name, payload, context)
        except Exception:
            return
