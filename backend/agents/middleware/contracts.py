"""Agent middleware contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from backend.agents.agent import Agent
from backend.agents.context import AgentContext
from backend.agents.models import AgentExecutionResult, AgentInput


@dataclass(frozen=True, slots=True)
class AgentExecutionRequest:
    """Request object passed through the agent middleware pipeline."""

    agent: Agent
    context: AgentContext
    agent_input: AgentInput
    state: dict[str, Any] = field(default_factory=dict)

    def with_context(self, context: AgentContext) -> AgentExecutionRequest:
        """Return a copy with an updated context."""
        return replace(self, context=context)

    def with_input(self, agent_input: AgentInput) -> AgentExecutionRequest:
        """Return a copy with an updated input."""
        return replace(self, agent_input=agent_input)

    def with_state(self, **values: Any) -> AgentExecutionRequest:
        """Return a copy with merged middleware state."""
        state = dict(self.state)
        state.update(values)
        return replace(self, state=state)

    def with_metadata(self, **values: Any) -> AgentExecutionRequest:
        """Return a copy with merged context metadata."""
        metadata = dict(self.context.metadata)
        metadata.update(values)
        return self.with_context(replace(self.context, metadata=metadata))


AgentMiddlewareNext = Callable[[AgentExecutionRequest], Awaitable[AgentExecutionResult]]


@runtime_checkable
class AgentExecutionMiddleware(Protocol):
    """Async middleware contract for agent execution."""

    async def __call__(
        self,
        request: AgentExecutionRequest,
        call_next: AgentMiddlewareNext,
    ) -> AgentExecutionResult:
        """Invoke the middleware and optionally continue the chain."""
        ...


@runtime_checkable
class AgentAuthenticator(Protocol):
    """Contract for authentication middleware dependencies."""

    async def authenticate(
        self,
        request: AgentExecutionRequest,
    ) -> Mapping[str, Any] | None:
        """Return identity information or `None` when rejected."""
        ...


@runtime_checkable
class AgentAuthorizer(Protocol):
    """Contract for authorization middleware dependencies."""

    async def authorize(
        self,
        request: AgentExecutionRequest,
        identity: Mapping[str, Any] | None,
    ) -> bool | None:
        """Return `True` when authorized."""
        ...


@runtime_checkable
class AgentPolicyEnforcer(Protocol):
    """Contract for policy middleware dependencies."""

    async def enforce(self, request: AgentExecutionRequest) -> None:
        """Raise when the request violates policy."""
        ...


@runtime_checkable
class AgentRateLimiter(Protocol):
    """Contract for rate limiting middleware dependencies."""

    async def acquire(self, request: AgentExecutionRequest) -> None:
        """Acquire capacity or raise when exhausted."""
        ...


@runtime_checkable
class AgentRequestValidator(Protocol):
    """Contract for request validation middleware dependencies."""

    def validate(self, request: AgentExecutionRequest) -> None:
        """Raise when invalid."""
        ...


@runtime_checkable
class AgentContextInjector(Protocol):
    """Contract for context injection middleware dependencies."""

    def inject(
        self,
        request: AgentExecutionRequest,
    ) -> AgentExecutionRequest | Mapping[str, Any] | None:
        """Return updated request or metadata to merge into it."""
        ...


@runtime_checkable
class AgentRecoveryHook(Protocol):
    """Contract for recovery middleware dependencies."""

    async def before(self, request: AgentExecutionRequest) -> None:
        """Hook executed before a recovery-capable execution begins."""
        ...

    async def after_success(
        self,
        request: AgentExecutionRequest,
        result: AgentExecutionResult,
    ) -> None:
        """Hook executed after successful execution."""
        ...

    async def after_failure(
        self,
        request: AgentExecutionRequest,
        error: BaseException,
    ) -> None:
        """Hook executed after failed execution."""
        ...


@runtime_checkable
class AgentMetricsRecorder(Protocol):
    """Contract for agent middleware metrics recording."""

    async def record_execution(
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
        """Record one agent execution observation."""
        ...

