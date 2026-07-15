"""Concrete runtime execution middleware implementations."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from backend.runtime.exceptions import RuntimeException
from backend.runtime.middleware.contracts import (
    ExecutionAuthenticator,
    ExecutionAuthorizer,
    ExecutionContextInjector,
    ExecutionMetricsRecorder,
    ExecutionPolicyEnforcer,
    ExecutionRateLimiter,
    ExecutionRecoveryHook,
    ExecutionRequestValidator,
    RuntimeExecutionRequest,
    RuntimeMiddlewareNext,
)
from backend.runtime.results import RuntimeResult


class AuthenticationError(RuntimeException):
    """Raised when authentication rejects an execution request."""


class AuthorizationError(RuntimeException):
    """Raised when authorization rejects an execution request."""


class PolicyViolationError(RuntimeException):
    """Raised when policy enforcement rejects an execution request."""


class RateLimitExceededError(RuntimeException):
    """Raised when a rate limit prevents execution."""


class RequestValidationError(RuntimeException):
    """Raised when request validation fails."""


class RecoveryHookError(RuntimeException):
    """Raised when a recovery hook fails."""


@dataclass(slots=True)
class AuthenticationMiddleware:
    """Authenticate the execution request before runtime invocation."""

    authenticator: ExecutionAuthenticator | None = None
    metadata_key: str = "authentication"

    async def __call__(
        self,
        request: RuntimeExecutionRequest,
        call_next: RuntimeMiddlewareNext,
    ) -> RuntimeResult:
        if self.authenticator is None:
            return await call_next(request)
        identity = await self.authenticator.authenticate(request)
        if identity is None:
            raise AuthenticationError(
                "Execution request is not authenticated",
                runtime_name=request.runtime.name,
                workflow_id=request.context.workflow.workflow_id,
                execution_id=request.context.execution.execution_id,
                attempt=request.context.attempt,
                retryable=False,
            )
        updated_request = request.with_state(identity=dict(identity))
        updated_request = updated_request.with_metadata(
            **{self.metadata_key: dict(identity)},
        )
        return await call_next(updated_request)


@dataclass(slots=True)
class AuthorizationMiddleware:
    """Authorize the execution request after authentication."""

    authorizer: ExecutionAuthorizer | None = None
    identity_state_key: str = "identity"

    async def __call__(
        self,
        request: RuntimeExecutionRequest,
        call_next: RuntimeMiddlewareNext,
    ) -> RuntimeResult:
        if self.authorizer is None:
            return await call_next(request)
        identity = request.state.get(self.identity_state_key)
        allowed = await self.authorizer.authorize(request, identity)
        if allowed is False:
            raise AuthorizationError(
                "Execution request is not authorized",
                runtime_name=request.runtime.name,
                workflow_id=request.context.workflow.workflow_id,
                execution_id=request.context.execution.execution_id,
                attempt=request.context.attempt,
                retryable=False,
            )
        return await call_next(request)


@dataclass(slots=True)
class LoggingMiddleware:
    """Log workflow execution lifecycle events."""

    logger: logging.Logger | None = None

    async def __call__(
        self,
        request: RuntimeExecutionRequest,
        call_next: RuntimeMiddlewareNext,
    ) -> RuntimeResult:
        logger = self.logger or logging.getLogger(__name__)
        logger.info(
            "workflow execution started",
            extra={
                "workflow_id": request.context.workflow.workflow_id,
                "execution_id": request.context.execution.execution_id,
                "correlation_id": request.context.correlation_id,
            },
        )
        try:
            result = await call_next(request)
        except Exception:
            logger.exception(
                "workflow execution failed",
                extra={
                    "workflow_id": request.context.workflow.workflow_id,
                    "execution_id": request.context.execution.execution_id,
                    "correlation_id": request.context.correlation_id,
                },
            )
            raise
        logger.info(
            "workflow execution completed",
            extra={
                "workflow_id": request.context.workflow.workflow_id,
                "execution_id": request.context.execution.execution_id,
                "correlation_id": request.context.correlation_id,
                "status": result.status,
            },
        )
        return result


@dataclass(slots=True)
class OpenTelemetryMiddleware:
    """Attach workflow execution spans and attributes."""

    tracer: Any = None

    async def __call__(
        self,
        request: RuntimeExecutionRequest,
        call_next: RuntimeMiddlewareNext,
    ) -> RuntimeResult:
        tracer = self.tracer or trace.get_tracer("sentinel.runtime.middleware")
        span_name = f"workflow.execute:{request.runtime.name}"
        with tracer.start_as_current_span(span_name) as span:
            span.set_attribute("workflow.id", request.context.workflow.workflow_id)
            span.set_attribute("execution.id", request.context.execution.execution_id)
            if request.context.correlation_id is not None:
                span.set_attribute("correlation.id", request.context.correlation_id)
            if request.context.workflow.version is not None:
                span.set_attribute("workflow.version", request.context.workflow.version)
            try:
                result = await call_next(request)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                raise
            span.set_attribute("execution.status", result.status)
            return result


@dataclass(slots=True)
class MetricsMiddleware:
    """Record execution metrics after the runtime completes."""

    recorder: ExecutionMetricsRecorder | None = None

    async def __call__(
        self,
        request: RuntimeExecutionRequest,
        call_next: RuntimeMiddlewareNext,
    ) -> RuntimeResult:
        started_at = datetime.now(UTC)
        result = await call_next(request)
        ended_at = datetime.now(UTC)
        duration_ms = max((ended_at - started_at).total_seconds() * 1000.0, 0.0)
        if self.recorder is not None:
            await self.recorder.record_execution(
                runtime_name=request.runtime.name,
                workflow_id=request.context.workflow.workflow_id,
                execution_id=request.context.execution.execution_id,
                status=result.status,
                duration_ms=duration_ms,
                attempt=request.context.attempt,
                correlation_id=request.context.correlation_id,
            )
        metadata = dict(result.metadata)
        metadata.setdefault("execution_duration_ms", duration_ms)
        return replace(result, metadata=metadata)


@dataclass(slots=True)
class PolicyEnforcementMiddleware:
    """Enforce execution policies before runtime execution."""

    policy_enforcer: ExecutionPolicyEnforcer | None = None

    async def __call__(
        self,
        request: RuntimeExecutionRequest,
        call_next: RuntimeMiddlewareNext,
    ) -> RuntimeResult:
        if self.policy_enforcer is not None:
            await self.policy_enforcer.enforce(request)
        return await call_next(request)


@dataclass(slots=True)
class RateLimitingMiddleware:
    """Acquire rate-limit capacity before executing a workflow."""

    rate_limiter: ExecutionRateLimiter | None = None

    async def __call__(
        self,
        request: RuntimeExecutionRequest,
        call_next: RuntimeMiddlewareNext,
    ) -> RuntimeResult:
        if self.rate_limiter is not None:
            await self.rate_limiter.acquire(request)
        return await call_next(request)


@dataclass(slots=True)
class ExecutionTimingMiddleware:
    """Record timing information on each execution result."""

    metadata_key: str = "execution_duration_ms"

    async def __call__(
        self,
        request: RuntimeExecutionRequest,
        call_next: RuntimeMiddlewareNext,
    ) -> RuntimeResult:
        started_at = datetime.now(UTC)
        result = await call_next(request)
        ended_at = datetime.now(UTC)
        duration_ms = max((ended_at - started_at).total_seconds() * 1000.0, 0.0)
        metadata = dict(result.metadata)
        metadata[self.metadata_key] = duration_ms
        return replace(result, metadata=metadata)


@dataclass(slots=True)
class RequestValidationMiddleware:
    """Validate runtime execution requests before dispatch."""

    validator: ExecutionRequestValidator | None = None

    async def __call__(
        self,
        request: RuntimeExecutionRequest,
        call_next: RuntimeMiddlewareNext,
    ) -> RuntimeResult:
        if self.validator is not None:
            self.validator.validate(request)
        return await call_next(request)


@dataclass(slots=True)
class ContextInjectionMiddleware:
    """Inject derived context values into the runtime execution request."""

    injectors: tuple[ExecutionContextInjector, ...] = ()

    async def __call__(
        self,
        request: RuntimeExecutionRequest,
        call_next: RuntimeMiddlewareNext,
    ) -> RuntimeResult:
        updated_request = request
        for injector in self.injectors:
            injected = injector.inject(updated_request)
            if injected is None:
                continue
            if isinstance(injected, RuntimeExecutionRequest):
                updated_request = injected
                continue
            updated_request = updated_request.with_metadata(**dict(injected))
        return await call_next(updated_request)


@dataclass(slots=True)
class ExecutionRecoveryMiddleware:
    """Run recovery hooks around checkpointed or retryable executions."""

    hook: ExecutionRecoveryHook | None = None

    async def __call__(
        self,
        request: RuntimeExecutionRequest,
        call_next: RuntimeMiddlewareNext,
    ) -> RuntimeResult:
        should_recover = (
            request.context.execution.checkpoint_id is not None
            or request.context.attempt > 1
        )
        if self.hook is None or not should_recover:
            return await call_next(request)
        try:
            await self.hook.before_execution(request)
        except Exception as exc:
            raise RecoveryHookError(
                "Recovery hook failed before execution",
                runtime_name=request.runtime.name,
                workflow_id=request.context.workflow.workflow_id,
                execution_id=request.context.execution.execution_id,
                attempt=request.context.attempt,
                retryable=False,
                metadata={"hook": "before_execution", "exception": type(exc).__name__},
            ) from exc
        try:
            result = await call_next(request)
        except Exception as exc:
            try:
                await self.hook.after_failure(request, exc)
            except Exception as hook_exc:
                raise RecoveryHookError(
                    "Recovery hook failed after execution failure",
                    runtime_name=request.runtime.name,
                    workflow_id=request.context.workflow.workflow_id,
                    execution_id=request.context.execution.execution_id,
                    attempt=request.context.attempt,
                    retryable=False,
                    metadata={
                        "hook": "after_failure",
                        "exception": type(hook_exc).__name__,
                    },
                ) from hook_exc
            raise
        try:
            await self.hook.after_success(request, result)
        except Exception as exc:
            raise RecoveryHookError(
                "Recovery hook failed after execution success",
                runtime_name=request.runtime.name,
                workflow_id=request.context.workflow.workflow_id,
                execution_id=request.context.execution.execution_id,
                attempt=request.context.attempt,
                retryable=False,
                metadata={"hook": "after_success", "exception": type(exc).__name__},
            ) from exc
        return result
