"""Concrete tool execution middleware implementations."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from backend.tools.exceptions import ToolException
from backend.tools.middleware.contracts import ToolExecutionRequest, ToolMiddlewareNext
from backend.tools.models import ToolResult
from backend.tools.validator import ToolValidator


@dataclass(slots=True)
class PermissionMiddleware:
    """Enforce tool permissions before execution."""

    validator: ToolValidator

    async def __call__(
        self,
        request: ToolExecutionRequest,
        call_next: ToolMiddlewareNext,
    ) -> ToolResult:
        self.validator.validate_permissions(request.tool.metadata, request.context)
        return await call_next(request)


@dataclass(slots=True)
class SchemaValidationMiddleware:
    """Validate tool input/output schemas."""

    validator: ToolValidator

    async def __call__(
        self,
        request: ToolExecutionRequest,
        call_next: ToolMiddlewareNext,
    ) -> ToolResult:
        self.validator.validate_input(request.tool.metadata, request.tool_input)
        result = await call_next(request)
        self.validator.validate_output(request.tool.metadata, result.output)
        return result


@dataclass(slots=True)
class LoggingMiddleware:
    """Log tool execution lifecycle."""

    logger: logging.Logger | None = None

    async def __call__(
        self,
        request: ToolExecutionRequest,
        call_next: ToolMiddlewareNext,
    ) -> ToolResult:
        logger = self.logger or logging.getLogger(__name__)
        logger.info(
            "tool execution started",
            extra={
                "tool": request.tool.name,
                "workflow_id": request.context.workflow_id,
                "execution_id": request.context.execution_id,
                "correlation_id": request.context.correlation_id,
            },
        )
        try:
            result = await call_next(request)
        except Exception:
            logger.exception(
                "tool execution failed",
                extra={
                    "tool": request.tool.name,
                    "workflow_id": request.context.workflow_id,
                    "execution_id": request.context.execution_id,
                    "correlation_id": request.context.correlation_id,
                },
            )
            raise
        logger.info(
            "tool execution completed",
            extra={
                "tool": request.tool.name,
                "workflow_id": request.context.workflow_id,
                "execution_id": request.context.execution_id,
                "correlation_id": request.context.correlation_id,
                "status": result.status,
            },
        )
        return result


@dataclass(slots=True)
class OpenTelemetryMiddleware:
    """Attach spans and attributes for tool execution."""

    tracer: Any = None

    async def __call__(
        self,
        request: ToolExecutionRequest,
        call_next: ToolMiddlewareNext,
    ) -> ToolResult:
        tracer = self.tracer or trace.get_tracer("sentinel.tool.middleware")
        span_name = f"tool.execute:{request.tool.name}"
        with tracer.start_as_current_span(span_name) as span:
            span.set_attribute("tool.name", request.tool.name)
            if request.tool.version is not None:
                span.set_attribute("tool.version", request.tool.version)
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
            span.set_attribute("tool.status", str(result.status))
            return result


@dataclass(slots=True)
class TimingMiddleware:
    """Attach tool execution duration to the result metadata."""

    metadata_key: str = "tool_duration_ms"

    async def __call__(
        self,
        request: ToolExecutionRequest,
        call_next: ToolMiddlewareNext,
    ) -> ToolResult:
        started_at = datetime.now(UTC)
        result = await call_next(request)
        ended_at = datetime.now(UTC)
        duration_ms = max((ended_at - started_at).total_seconds() * 1000.0, 0.0)
        metadata = dict(result.metadata)
        metadata[self.metadata_key] = duration_ms
        return replace(result, metadata=metadata)


@dataclass(slots=True)
class MetricsMiddleware:
    """Record tool execution metrics after completion."""

    async def __call__(
        self,
        request: ToolExecutionRequest,
        call_next: ToolMiddlewareNext,
    ) -> ToolResult:
        started_at = datetime.now(UTC)
        result = await call_next(request)
        ended_at = datetime.now(UTC)
        duration_ms = max((ended_at - started_at).total_seconds() * 1000.0, 0.0)
        if request.context.metrics_recorder is not None:
            await request.context.metrics_recorder.record_tool_execution(
                tool_name=request.tool.name,
                tool_version=request.tool.version,
                workflow_id=request.context.workflow_id,
                execution_id=request.context.execution_id,
                status=str(result.status),
                duration_ms=duration_ms,
                attempt=request.context.attempt,
                correlation_id=request.context.correlation_id,
            )
        metadata = dict(result.metadata)
        metadata.setdefault("tool_duration_ms", duration_ms)
        return replace(result, metadata=metadata)


@dataclass(slots=True)
class EvaluationMiddleware:
    """Execute evaluation hooks around tool execution."""

    async def __call__(
        self,
        request: ToolExecutionRequest,
        call_next: ToolMiddlewareNext,
    ) -> ToolResult:
        hook = request.context.evaluation_hook
        if hook is None:
            return await call_next(request)
        await hook.before(request.context)
        result = await call_next(request)
        await hook.after(request.context, status=str(result.status))
        return result


@dataclass(slots=True)
class RequestValidationMiddleware:
    """Validate that the request is structurally valid."""

    async def __call__(
        self,
        request: ToolExecutionRequest,
        call_next: ToolMiddlewareNext,
    ) -> ToolResult:
        metadata = request.tool.metadata
        if not metadata.name.strip():
            raise ToolException("Tool name is required")
        return await call_next(request)
