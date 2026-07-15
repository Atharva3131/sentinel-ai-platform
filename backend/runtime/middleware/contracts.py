"""Runtime middleware contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from backend.runtime.context import RuntimeContext
from backend.runtime.contracts import WorkflowRuntime
from backend.runtime.results import RuntimeResult


@dataclass(frozen=True, slots=True)
class RuntimeExecutionRequest:
    """Normalized request object passed through the middleware pipeline."""

    runtime: WorkflowRuntime
    context: RuntimeContext
    state: dict[str, Any] = field(default_factory=dict)

    def with_context(self, context: RuntimeContext) -> RuntimeExecutionRequest:
        """Return a copy with an updated runtime context."""
        return replace(self, context=context)

    def with_state(self, **values: Any) -> RuntimeExecutionRequest:
        """Return a copy with merged middleware state."""
        state = dict(self.state)
        state.update(values)
        return replace(self, state=state)

    def with_metadata(self, **values: Any) -> RuntimeExecutionRequest:
        """Return a copy with merged context metadata."""
        metadata = dict(self.context.metadata)
        metadata.update(values)
        return self.with_context(replace(self.context, metadata=metadata))


RuntimeMiddlewareNext = Callable[[RuntimeExecutionRequest], Awaitable[RuntimeResult]]


@runtime_checkable
class RuntimeExecutionMiddleware(Protocol):
    """Async middleware contract for workflow runtime execution."""

    async def __call__(
        self,
        request: RuntimeExecutionRequest,
        call_next: RuntimeMiddlewareNext,
    ) -> RuntimeResult:
        """Invoke the middleware and optionally continue the chain."""
        ...


@runtime_checkable
class ExecutionAuthenticator(Protocol):
    """Contract for authentication middleware dependencies."""

    async def authenticate(
        self,
        request: RuntimeExecutionRequest,
    ) -> Mapping[str, Any] | None:
        """Return authenticated identity information or `None` when rejected."""
        ...


@runtime_checkable
class ExecutionAuthorizer(Protocol):
    """Contract for authorization middleware dependencies."""

    async def authorize(
        self,
        request: RuntimeExecutionRequest,
        identity: Mapping[str, Any] | None,
    ) -> bool | None:
        """Return `True` when the request is authorized."""
        ...


@runtime_checkable
class ExecutionPolicyEnforcer(Protocol):
    """Contract for policy middleware dependencies."""

    async def enforce(self, request: RuntimeExecutionRequest) -> None:
        """Raise when the request violates policy."""
        ...


@runtime_checkable
class ExecutionRateLimiter(Protocol):
    """Contract for rate limiting middleware dependencies."""

    async def acquire(self, request: RuntimeExecutionRequest) -> None:
        """Acquire a rate-limit token or raise when exhausted."""
        ...


@runtime_checkable
class ExecutionRequestValidator(Protocol):
    """Contract for request validation middleware dependencies."""

    def validate(self, request: RuntimeExecutionRequest) -> None:
        """Raise when the execution request is invalid."""
        ...


@runtime_checkable
class ExecutionContextInjector(Protocol):
    """Contract for context injection middleware dependencies."""

    def inject(
        self,
        request: RuntimeExecutionRequest,
    ) -> RuntimeExecutionRequest | Mapping[str, Any] | None:
        """Return an updated request or metadata to merge into it."""
        ...


@runtime_checkable
class ExecutionRecoveryHook(Protocol):
    """Contract for recovery middleware dependencies."""

    async def before_execution(self, request: RuntimeExecutionRequest) -> None:
        """Hook executed before a recovery-capable execution begins."""
        ...

    async def after_success(
        self,
        request: RuntimeExecutionRequest,
        result: RuntimeResult,
    ) -> None:
        """Hook executed after a successful execution."""
        ...

    async def after_failure(
        self,
        request: RuntimeExecutionRequest,
        error: BaseException,
    ) -> None:
        """Hook executed after a failed execution."""
        ...


@runtime_checkable
class ExecutionMetricsRecorder(Protocol):
    """Contract for execution metrics middleware dependencies."""

    async def record_execution(
        self,
        *,
        runtime_name: str,
        workflow_id: str,
        execution_id: str,
        status: str,
        duration_ms: float,
        attempt: int,
        correlation_id: str | None,
    ) -> None:
        """Record a workflow execution observation."""
        ...

