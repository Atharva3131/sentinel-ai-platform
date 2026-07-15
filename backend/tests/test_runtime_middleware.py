"""Runtime middleware pipeline tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from backend.runtime import RuntimeContext, RuntimeException, RuntimeFactory, RuntimeRegistry
from backend.runtime.middleware import (
    AuthenticationMiddleware,
    AuthorizationMiddleware,
    ContextInjectionMiddleware,
    ExecutionRecoveryMiddleware,
    ExecutionTimingMiddleware,
    LoggingMiddleware,
    MetricsMiddleware,
    OpenTelemetryMiddleware,
    PolicyEnforcementMiddleware,
    RateLimitingMiddleware,
    RequestValidationMiddleware,
    RuntimeExecutionRequest,
    RuntimeMiddlewarePipeline,
)
from backend.runtime.results import RuntimeResult
from backend.workflows import (
    ExecutionPolicy,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowExecutor,
    WorkflowLifecycle,
)
from backend.workflows import (
    WorkflowRegistry as WorkflowDefinitionRegistry,
)


class FakeRuntime:
    """Runtime double that records the received context."""

    def __init__(self) -> None:
        self.contexts: list[RuntimeContext] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def version(self) -> str | None:
        return "1.0.0"

    @property
    def capabilities(self) -> tuple[str, ...]:
        return ("generic",)

    def supports(self, workflow: Any) -> bool:
        return True

    async def execute(self, context: RuntimeContext) -> RuntimeResult:
        self.contexts.append(context)
        return RuntimeResult(
            workflow=context.workflow,
            execution=context.execution,
            status="completed",
            metadata={"from_runtime": True},
        )

    async def cancel(self, context: RuntimeContext) -> None:
        return None


@dataclass(slots=True)
class FakeAuthenticator:
    calls: list[str]

    async def authenticate(
        self,
        request: RuntimeExecutionRequest,
    ) -> Mapping[str, Any] | None:
        self.calls.append("authenticate")
        return {"subject": "user-1"}


@dataclass(slots=True)
class FakeAuthorizer:
    calls: list[tuple[str, Mapping[str, Any] | None]]

    async def authorize(
        self,
        request: RuntimeExecutionRequest,
        identity: Mapping[str, Any] | None,
    ) -> bool | None:
        self.calls.append(("authorize", identity))
        return True


@dataclass(slots=True)
class FakeInjector:
    calls: list[str]

    def inject(
        self,
        request: RuntimeExecutionRequest,
    ) -> RuntimeExecutionRequest | Mapping[str, Any] | None:
        self.calls.append("inject")
        return {"injected": "yes"}


@dataclass(slots=True)
class FakePolicy:
    calls: list[str]

    async def enforce(self, request: RuntimeExecutionRequest) -> None:
        self.calls.append("policy")


@dataclass(slots=True)
class FakeRateLimiter:
    calls: list[str]

    async def acquire(self, request: RuntimeExecutionRequest) -> None:
        self.calls.append("rate_limit")


@dataclass(slots=True)
class FakeValidator:
    calls: list[str]

    def validate(self, request: RuntimeExecutionRequest) -> None:
        self.calls.append("validate")


@dataclass(slots=True)
class FakeMetricsRecorder:
    calls: list[dict[str, Any]]

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
        self.calls.append(
            {
                "runtime_name": runtime_name,
                "workflow_id": workflow_id,
                "execution_id": execution_id,
                "status": status,
                "duration_ms": duration_ms,
                "attempt": attempt,
                "correlation_id": correlation_id,
            }
        )


@dataclass(slots=True)
class FakeRecoveryHook:
    calls: list[tuple[str, str]]

    async def before_execution(self, request: RuntimeExecutionRequest) -> None:
        self.calls.append(("before", request.context.execution.execution_id))

    async def after_success(
        self,
        request: RuntimeExecutionRequest,
        result: RuntimeResult,
    ) -> None:
        self.calls.append(("success", result.status))

    async def after_failure(
        self,
        request: RuntimeExecutionRequest,
        error: BaseException,
    ) -> None:
        self.calls.append(("failure", type(error).__name__))


def _workflow_definition() -> WorkflowBuilder:
    return (
        WorkflowBuilder(workflow_id="wf-1", name="incident", version="1.0.0")
        .with_runtime("fake")
        .with_execution_policy(
            ExecutionPolicy(max_attempts=1, allow_recovery=True)
        )
        .with_lifecycle(WorkflowLifecycle())
    )


def _runtime_context() -> RuntimeContext:
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
        attempt=2,
        checkpoint_id="ckpt-1",
    )
    return RuntimeContext(
        workflow=workflow,
        execution=execution,
        correlation_id="corr-1",
        attempt=2,
        max_attempts=2,
        metadata={"seed": "value"},
    )


@pytest.mark.asyncio
async def test_runtime_middleware_pipeline_wraps_execution() -> None:
    auth_calls: list[str] = []
    authorizer_calls: list[tuple[str, Mapping[str, Any] | None]] = []
    injector_calls: list[str] = []
    policy_calls: list[str] = []
    rate_calls: list[str] = []
    validator_calls: list[str] = []
    metrics_calls: list[dict[str, Any]] = []
    recovery_calls: list[tuple[str, str]] = []

    pipeline = RuntimeMiddlewarePipeline(
        middlewares=(
            RequestValidationMiddleware(FakeValidator(validator_calls)),
            ContextInjectionMiddleware((FakeInjector(injector_calls),)),
            AuthenticationMiddleware(FakeAuthenticator(auth_calls)),
            AuthorizationMiddleware(FakeAuthorizer(authorizer_calls)),
            PolicyEnforcementMiddleware(FakePolicy(policy_calls)),
            RateLimitingMiddleware(FakeRateLimiter(rate_calls)),
            ExecutionRecoveryMiddleware(FakeRecoveryHook(recovery_calls)),
            LoggingMiddleware(),
            MetricsMiddleware(FakeMetricsRecorder(metrics_calls)),
            OpenTelemetryMiddleware(),
            ExecutionTimingMiddleware(),
        )
    )
    runtime = FakeRuntime()
    request = RuntimeExecutionRequest(runtime=runtime, context=_runtime_context())

    result = await pipeline.execute(runtime, request)

    assert result.status == "completed"
    assert runtime.contexts[0].metadata["injected"] == "yes"
    assert runtime.contexts[0].metadata["authentication"]["subject"] == "user-1"
    assert validator_calls == ["validate"]
    assert injector_calls == ["inject"]
    assert auth_calls == ["authenticate"]
    assert authorizer_calls == [("authorize", {"subject": "user-1"})]
    assert policy_calls == ["policy"]
    assert rate_calls == ["rate_limit"]
    assert recovery_calls == [("before", "ex-1"), ("success", "completed")]
    assert metrics_calls[0]["status"] == "completed"
    assert result.metadata["from_runtime"] is True
    assert "execution_duration_ms" in result.metadata


@pytest.mark.asyncio
async def test_workflow_executor_uses_runtime_middleware_pipeline() -> None:
    runtime = FakeRuntime()
    runtime_registry = RuntimeRegistry()
    runtime_registry.register("fake", runtime, default=True)
    executor = WorkflowExecutor(
        workflow_registry=WorkflowDefinitionRegistry(),
        runtime_factory=RuntimeFactory(runtime_registry),
        middleware_pipeline=RuntimeMiddlewarePipeline(
            middlewares=(
                AuthenticationMiddleware(FakeAuthenticator([])),
                ContextInjectionMiddleware((FakeInjector([]),)),
            )
        ),
    )
    definition = _workflow_definition().build()
    executor.workflow_registry.register(definition, default=True)
    context = WorkflowContext(
        workflow_id="wf-1",
        workflow_version="1.0.0",
        execution_id="ex-1",
        runtime_name="fake",
    )

    state = await executor.execute(context)

    assert state.status == "completed"
    assert runtime.contexts[0].metadata["injected"] == "yes"
    assert runtime.contexts[0].metadata["authentication"]["subject"] == "user-1"


@pytest.mark.asyncio
async def test_request_validation_stops_execution() -> None:
    class RejectingValidator:
        def validate(self, request: RuntimeExecutionRequest) -> None:
            raise RuntimeException("invalid request")

    runtime = FakeRuntime()
    pipeline = RuntimeMiddlewarePipeline(
        middlewares=(RequestValidationMiddleware(RejectingValidator()),)
    )
    request = RuntimeExecutionRequest(runtime=runtime, context=_runtime_context())

    with pytest.raises(RuntimeException):
        await pipeline.execute(runtime, request)

    assert runtime.contexts == []
