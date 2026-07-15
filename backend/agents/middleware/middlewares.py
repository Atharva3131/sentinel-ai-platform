"""Concrete agent execution middleware implementations."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from backend.agents.context import AgentEvaluationHook
from backend.agents.exceptions import AgentException
from backend.agents.middleware.contracts import (
    AgentAuthenticator,
    AgentAuthorizer,
    AgentContextInjector,
    AgentExecutionRequest,
    AgentMetricsRecorder,
    AgentMiddlewareNext,
    AgentPolicyEnforcer,
    AgentRateLimiter,
    AgentRecoveryHook,
    AgentRequestValidator,
)
from backend.agents.models import AgentExecutionResult


class AuthenticationError(AgentException):
    """Raised when authentication rejects an agent execution request."""


class AuthorizationError(AgentException):
    """Raised when authorization rejects an agent execution request."""


class PolicyViolationError(AgentException):
    """Raised when policy enforcement rejects an agent execution request."""


class RateLimitExceededError(AgentException):
    """Raised when a rate limit prevents agent execution."""


class RequestValidationError(AgentException):
    """Raised when request validation fails."""


class RecoveryHookError(AgentException):
    """Raised when a recovery hook fails."""


@dataclass(slots=True)
class AuthenticationMiddleware:
    """Authenticate an agent execution request."""

    authenticator: AgentAuthenticator | None = None
    metadata_key: str = "authentication"

    async def __call__(
        self,
        request: AgentExecutionRequest,
        call_next: AgentMiddlewareNext,
    ) -> AgentExecutionResult:
        if self.authenticator is None:
            return await call_next(request)
        identity = await self.authenticator.authenticate(request)
        if identity is None:
            raise AuthenticationError(
                "Agent execution is not authenticated",
                agent_name=request.agent.name,
                agent_version=request.agent.version,
                workflow_id=request.context.workflow_id,
                execution_id=request.context.execution_id,
                attempt=request.context.attempt,
                retryable=False,
            )
        updated_request = request.with_state(identity=dict(identity)).with_metadata(
            **{self.metadata_key: dict(identity)},
        )
        return await call_next(updated_request)


@dataclass(slots=True)
class AuthorizationMiddleware:
    """Authorize an agent execution request."""

    authorizer: AgentAuthorizer | None = None
    identity_state_key: str = "identity"

    async def __call__(
        self,
        request: AgentExecutionRequest,
        call_next: AgentMiddlewareNext,
    ) -> AgentExecutionResult:
        if self.authorizer is None:
            return await call_next(request)
        identity = request.state.get(self.identity_state_key)
        allowed = await self.authorizer.authorize(request, identity)
        if allowed is False:
            raise AuthorizationError(
                "Agent execution is not authorized",
                agent_name=request.agent.name,
                agent_version=request.agent.version,
                workflow_id=request.context.workflow_id,
                execution_id=request.context.execution_id,
                attempt=request.context.attempt,
                retryable=False,
            )
        return await call_next(request)


@dataclass(slots=True)
class LoggingMiddleware:
    """Log agent execution lifecycle."""

    logger: logging.Logger | None = None

    async def __call__(
        self,
        request: AgentExecutionRequest,
        call_next: AgentMiddlewareNext,
    ) -> AgentExecutionResult:
        logger = self.logger or logging.getLogger(__name__)
        logger.info(
            "agent execution started",
            extra={
                "agent": request.agent.name,
                "workflow_id": request.context.workflow_id,
                "execution_id": request.context.execution_id,
                "correlation_id": request.context.correlation_id,
            },
        )
        try:
            result = await call_next(request)
        except Exception:
            logger.exception(
                "agent execution failed",
                extra={
                    "agent": request.agent.name,
                    "workflow_id": request.context.workflow_id,
                    "execution_id": request.context.execution_id,
                    "correlation_id": request.context.correlation_id,
                },
            )
            raise
        logger.info(
            "agent execution completed",
            extra={
                "agent": request.agent.name,
                "workflow_id": request.context.workflow_id,
                "execution_id": request.context.execution_id,
                "correlation_id": request.context.correlation_id,
                "status": result.status,
            },
        )
        return result


@dataclass(slots=True)
class OpenTelemetryMiddleware:
    """Attach spans and attributes for agent execution."""

    tracer: Any = None

    async def __call__(
        self,
        request: AgentExecutionRequest,
        call_next: AgentMiddlewareNext,
    ) -> AgentExecutionResult:
        tracer = self.tracer or trace.get_tracer("sentinel.agent.middleware")
        span_name = f"agent.execute:{request.agent.name}"
        with tracer.start_as_current_span(span_name) as span:
            span.set_attribute("agent.name", request.agent.name)
            if request.agent.version is not None:
                span.set_attribute("agent.version", request.agent.version)
            if request.context.workflow_id is not None:
                span.set_attribute("workflow.id", request.context.workflow_id)
            if request.context.execution_id is not None:
                span.set_attribute("execution.id", request.context.execution_id)
            if request.context.correlation_id is not None:
                span.set_attribute("correlation.id", request.context.correlation_id)
            try:
                result = await call_next(request)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                raise
            span.set_attribute("agent.status", str(result.status))
            return result


@dataclass(slots=True)
class MetricsMiddleware:
    """Record agent execution metrics after completion."""

    recorder: AgentMetricsRecorder | None = None

    async def __call__(
        self,
        request: AgentExecutionRequest,
        call_next: AgentMiddlewareNext,
    ) -> AgentExecutionResult:
        started_at = datetime.now(UTC)
        result = await call_next(request)
        ended_at = datetime.now(UTC)
        duration_ms = max((ended_at - started_at).total_seconds() * 1000.0, 0.0)
        recorder = self.recorder
        if recorder is not None:
            await recorder.record_execution(
                agent_name=request.agent.name,
                agent_version=request.agent.version,
                workflow_id=request.context.workflow_id,
                execution_id=request.context.execution_id,
                status=str(result.status),
                duration_ms=duration_ms,
                attempt=request.context.attempt,
                correlation_id=request.context.correlation_id,
            )
        elif request.context.metrics_recorder is not None:
            await request.context.metrics_recorder.record_agent_execution(
                agent_name=request.agent.name,
                agent_version=request.agent.version,
                workflow_id=request.context.workflow_id,
                execution_id=request.context.execution_id,
                status=str(result.status),
                duration_ms=duration_ms,
                attempt=request.context.attempt,
                correlation_id=request.context.correlation_id,
            )
        metadata = dict(result.metadata)
        metadata.setdefault("agent_duration_ms", duration_ms)
        return AgentExecutionResult(
            agent=result.agent,
            workflow_id=result.workflow_id,
            execution_id=result.execution_id,
            status=result.status,
            output=result.output,
            error=result.error,
            retryable=result.retryable,
            retry_after_seconds=result.retry_after_seconds,
            started_at=result.started_at,
            ended_at=result.ended_at,
            emitted_events=result.emitted_events,
            metadata=metadata,
        )


@dataclass(slots=True)
class PolicyEnforcementMiddleware:
    """Enforce policy before agent execution."""

    policy_enforcer: AgentPolicyEnforcer | None = None

    async def __call__(
        self,
        request: AgentExecutionRequest,
        call_next: AgentMiddlewareNext,
    ) -> AgentExecutionResult:
        if self.policy_enforcer is not None:
            await self.policy_enforcer.enforce(request)
        return await call_next(request)


@dataclass(slots=True)
class RateLimitingMiddleware:
    """Acquire rate-limit capacity before agent execution."""

    rate_limiter: AgentRateLimiter | None = None

    async def __call__(
        self,
        request: AgentExecutionRequest,
        call_next: AgentMiddlewareNext,
    ) -> AgentExecutionResult:
        if self.rate_limiter is not None:
            await self.rate_limiter.acquire(request)
        return await call_next(request)


@dataclass(slots=True)
class ExecutionTimingMiddleware:
    """Attach duration information to the result metadata."""

    metadata_key: str = "agent_duration_ms"

    async def __call__(
        self,
        request: AgentExecutionRequest,
        call_next: AgentMiddlewareNext,
    ) -> AgentExecutionResult:
        started_at = datetime.now(UTC)
        result = await call_next(request)
        ended_at = datetime.now(UTC)
        duration_ms = max((ended_at - started_at).total_seconds() * 1000.0, 0.0)
        metadata = dict(result.metadata)
        metadata[self.metadata_key] = duration_ms
        return AgentExecutionResult(
            agent=result.agent,
            workflow_id=result.workflow_id,
            execution_id=result.execution_id,
            status=result.status,
            output=result.output,
            error=result.error,
            retryable=result.retryable,
            retry_after_seconds=result.retry_after_seconds,
            started_at=result.started_at,
            ended_at=result.ended_at,
            emitted_events=result.emitted_events,
            metadata=metadata,
        )


@dataclass(slots=True)
class RequestValidationMiddleware:
    """Validate agent execution request before dispatch."""

    validator: AgentRequestValidator | None = None

    async def __call__(
        self,
        request: AgentExecutionRequest,
        call_next: AgentMiddlewareNext,
    ) -> AgentExecutionResult:
        if self.validator is not None:
            self.validator.validate(request)
        return await call_next(request)


@dataclass(slots=True)
class ContextInjectionMiddleware:
    """Inject derived metadata into the agent execution request."""

    injectors: tuple[AgentContextInjector, ...] = ()

    async def __call__(
        self,
        request: AgentExecutionRequest,
        call_next: AgentMiddlewareNext,
    ) -> AgentExecutionResult:
        updated_request = request
        for injector in self.injectors:
            injected = injector.inject(updated_request)
            if injected is None:
                continue
            if isinstance(injected, AgentExecutionRequest):
                updated_request = injected
                continue
            updated_request = updated_request.with_metadata(**dict(injected))
        return await call_next(updated_request)


@dataclass(slots=True)
class ExecutionRecoveryMiddleware:
    """Run recovery hooks around retryable or resumed agent execution."""

    hook: AgentRecoveryHook | None = None

    async def __call__(
        self,
        request: AgentExecutionRequest,
        call_next: AgentMiddlewareNext,
    ) -> AgentExecutionResult:
        should_recover = request.context.attempt > 1
        if self.hook is None or not should_recover:
            return await call_next(request)
        try:
            await self.hook.before(request)
        except Exception as exc:
            raise RecoveryHookError(
                "Recovery hook failed before agent execution",
                agent_name=request.agent.name,
                agent_version=request.agent.version,
                workflow_id=request.context.workflow_id,
                execution_id=request.context.execution_id,
                attempt=request.context.attempt,
                retryable=False,
                metadata={"hook": "before", "exception": type(exc).__name__},
            ) from exc
        try:
            result = await call_next(request)
        except Exception as exc:
            try:
                await self.hook.after_failure(request, exc)
            except Exception as hook_exc:
                raise RecoveryHookError(
                    "Recovery hook failed after agent failure",
                    agent_name=request.agent.name,
                    agent_version=request.agent.version,
                    workflow_id=request.context.workflow_id,
                    execution_id=request.context.execution_id,
                    attempt=request.context.attempt,
                    retryable=False,
                    metadata={"hook": "after_failure", "exception": type(hook_exc).__name__},
                ) from hook_exc
            raise
        try:
            await self.hook.after_success(request, result)
        except Exception as exc:
            raise RecoveryHookError(
                "Recovery hook failed after agent success",
                agent_name=request.agent.name,
                agent_version=request.agent.version,
                workflow_id=request.context.workflow_id,
                execution_id=request.context.execution_id,
                attempt=request.context.attempt,
                retryable=False,
                metadata={"hook": "after_success", "exception": type(exc).__name__},
            ) from exc
        return result


@dataclass(slots=True)
class AgentEvaluationMiddleware:
    """Execute evaluation hooks around agent execution."""

    hook: AgentEvaluationHook | None = None

    async def __call__(
        self,
        request: AgentExecutionRequest,
        call_next: AgentMiddlewareNext,
    ) -> AgentExecutionResult:
        hook = self.hook or request.context.evaluation_hook
        if hook is None:
            return await call_next(request)
        await hook.before(request.context)
        result = await call_next(request)
        await hook.after(request.context, status=str(result.status))
        return result
