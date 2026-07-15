"""Workflow executor that orchestrates runtime execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from backend.runtime import RuntimeException, RuntimeFactory, RuntimeResult
from backend.workflows.context import WorkflowContext
from backend.workflows.registry import WorkflowRegistry
from backend.workflows.state import WorkflowState
from backend.workflows.validator import WorkflowValidationError, WorkflowValidator


@dataclass(slots=True)
class WorkflowExecutor:
    """Execute versioned workflows through a pluggable runtime."""

    workflow_registry: WorkflowRegistry
    runtime_factory: RuntimeFactory
    validator: WorkflowValidator = field(default_factory=WorkflowValidator)

    async def execute(
        self,
        context: WorkflowContext,
        state: WorkflowState | None = None,
    ) -> WorkflowState:
        """Execute a workflow until it reaches a terminal state or exhausts retries."""
        self.validator.validate_context(context)
        definition = self.workflow_registry.resolve(context.workflow_id, context.workflow_version)
        self.validator.validate_definition(definition)
        if context.workflow_version is not None and context.workflow_version != definition.version:
            raise WorkflowValidationError("Workflow context version does not match definition")

        current_state = state or WorkflowState.initial(
            workflow_id=definition.workflow_id,
            workflow_version=definition.version,
            execution_id=context.execution_id,
            metadata=dict(context.metadata),
        )
        self.validator.validate_state(current_state, definition.lifecycle)

        if current_state.is_terminal(definition.lifecycle):
            return current_state
        if current_state.workflow_id != definition.workflow_id:
            raise WorkflowValidationError("Workflow state does not match definition")
        if current_state.workflow_version != definition.version:
            raise WorkflowValidationError("Workflow state version does not match definition")

        runtime = self.runtime_factory.resolve(context.runtime_name or definition.runtime_name)
        policy = definition.execution_policy
        attempt = max(context.attempt, current_state.attempt + 1, 1)

        while attempt <= policy.max_attempts:
            if context.cancellation_token is not None and context.cancellation_token.is_set():
                self.validator.validate_transition(
                    current_state,
                    definition.lifecycle.cancelled_status,
                    definition.lifecycle,
                )
                current_state = current_state.with_status(
                    definition.lifecycle.cancelled_status,
                    attempt=attempt,
                    ended_at=datetime.now(UTC),
                )
                await self._emit(
                    context,
                    "workflow.cancelled",
                    {
                        "workflow_id": definition.workflow_id,
                        "workflow_version": definition.version,
                        "execution_id": context.execution_id,
                        "attempt": attempt,
                    },
                )
                return current_state

            self.validator.validate_transition(
                current_state,
                definition.lifecycle.running_status,
                definition.lifecycle,
            )
            running_state = current_state.with_status(
                definition.lifecycle.running_status,
                attempt=attempt,
                started_at=current_state.started_at or datetime.now(UTC),
                clear_ended_at=True,
            )
            await self._emit(
                context,
                "workflow.started",
                {
                    "workflow_id": definition.workflow_id,
                    "workflow_version": definition.version,
                    "execution_id": context.execution_id,
                    "attempt": attempt,
                    "checkpoint_id": running_state.checkpoint_id,
                },
            )
            runtime_context = context.with_attempt(attempt).to_runtime_context(
                definition,
                running_state,
                runtime_name=runtime.name,
                timeout_seconds=policy.effective_timeout_seconds(context.timeout_seconds),
            )
            runtime_result = await self._execute_runtime(runtime, runtime_context)
            updated_state = running_state.with_result(runtime_result)
            updated_state = self._maybe_update_checkpoint(updated_state, runtime_result)

            if runtime_result.status == "completed":
                self.validator.validate_transition(
                    running_state,
                    definition.lifecycle.completed_status,
                    definition.lifecycle,
                )
                updated_state = updated_state.with_status(
                    definition.lifecycle.completed_status,
                    attempt=attempt,
                    ended_at=datetime.now(UTC),
                )
                await self._emit(
                    context,
                    "workflow.completed",
                    {
                        "workflow_id": definition.workflow_id,
                        "workflow_version": definition.version,
                        "execution_id": context.execution_id,
                        "attempt": attempt,
                    },
                )
                return updated_state

            if runtime_result.status in {"cancelled", "timed_out"}:
                terminal_status = (
                    definition.lifecycle.cancelled_status
                    if runtime_result.status == "cancelled"
                    else definition.lifecycle.timed_out_status
                )
                self.validator.validate_transition(
                    running_state,
                    terminal_status,
                    definition.lifecycle,
                )
                updated_state = updated_state.with_status(
                    terminal_status,
                    attempt=attempt,
                    ended_at=datetime.now(UTC),
                )
                await self._emit(
                    context,
                    f"workflow.{terminal_status}",
                    {
                        "workflow_id": definition.workflow_id,
                        "workflow_version": definition.version,
                        "execution_id": context.execution_id,
                        "attempt": attempt,
                    },
                )
                return updated_state

            if policy.can_retry(updated_state.status, attempt) and runtime_result.retryable:
                delay = runtime_result.retry_after_seconds
                if delay is None:
                    delay = policy.next_retry_delay_seconds(attempt)
                await self._emit(
                    context,
                    "workflow.retry_scheduled",
                    {
                        "workflow_id": definition.workflow_id,
                        "workflow_version": definition.version,
                        "execution_id": context.execution_id,
                        "attempt": attempt,
                        "retry_after_seconds": delay,
                    },
                )
                self.validator.validate_transition(
                    running_state,
                    definition.lifecycle.retrying_status,
                    definition.lifecycle,
                )
                updated_state = updated_state.with_status(
                    definition.lifecycle.retrying_status,
                    attempt=attempt + 1,
                    clear_ended_at=True,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                current_state = updated_state
                attempt += 1
                continue

            failed_status = definition.lifecycle.failed_status
            self.validator.validate_transition(
                running_state,
                failed_status,
                definition.lifecycle,
            )
            updated_state = updated_state.with_status(
                failed_status,
                attempt=attempt,
                ended_at=datetime.now(UTC),
                last_error=updated_state.last_error or "workflow execution failed",
            )
            await self._emit(
                context,
                "workflow.failed",
                {
                    "workflow_id": definition.workflow_id,
                    "workflow_version": definition.version,
                    "execution_id": context.execution_id,
                    "attempt": attempt,
                    "retryable": runtime_result.retryable,
                },
            )
            return updated_state

        return current_state.with_status(
            definition.lifecycle.failed_status,
            attempt=attempt,
            ended_at=datetime.now(UTC),
            last_error="retry budget exhausted",
        )

    async def resume(
        self,
        context: WorkflowContext,
        state: WorkflowState,
    ) -> WorkflowState:
        """Resume a paused or retryable workflow from the supplied state."""
        definition = self.workflow_registry.resolve(state.workflow_id, state.workflow_version)
        if not state.can_restart(definition.lifecycle):
            raise WorkflowValidationError("Workflow state cannot be resumed")
        return await self.execute(context, state)

    async def recover(
        self,
        context: WorkflowContext,
        state: WorkflowState,
    ) -> WorkflowState:
        """Recover a workflow from a checkpointed state."""
        definition = self.workflow_registry.resolve(state.workflow_id, state.workflow_version)
        if not definition.execution_policy.allow_recovery:
            raise WorkflowValidationError("Workflow recovery is disabled")
        checkpoint_id = state.checkpoint_id or (
            state.last_result.metadata.get("checkpoint_id")
            if state.last_result is not None
            else None
        )
        if checkpoint_id is None:
            raise WorkflowValidationError("Workflow recovery requires a checkpoint")
        recovered_state = state.with_recovery().with_status(
            definition.lifecycle.recovering_status,
            attempt=state.attempt + 1,
            checkpoint_id=checkpoint_id,
            clear_ended_at=True,
        )
        await self._emit(
            context,
            "workflow.recovery.started",
            {
                "workflow_id": state.workflow_id,
                "workflow_version": state.workflow_version,
                "execution_id": state.execution_id,
                "checkpoint_id": checkpoint_id,
            },
        )
        result = await self.execute(context, recovered_state)
        await self._emit(
            context,
            "workflow.recovery.completed",
            {
                "workflow_id": result.workflow_id,
                "workflow_version": result.workflow_version,
                "execution_id": result.execution_id,
                "status": result.status,
            },
        )
        return result

    async def cancel(
        self,
        context: WorkflowContext,
        state: WorkflowState,
    ) -> WorkflowState:
        """Cancel a workflow without invoking the runtime."""
        definition = self.workflow_registry.resolve(state.workflow_id, state.workflow_version)
        cancelled_state = state.with_status(
            definition.lifecycle.cancelled_status,
            ended_at=datetime.now(UTC),
        )
        await self._emit(
            context,
            "workflow.cancelled",
            {
                "workflow_id": state.workflow_id,
                "workflow_version": state.workflow_version,
                "execution_id": state.execution_id,
            },
        )
        return cancelled_state

    async def _execute_runtime(
        self,
        runtime: Any,
        runtime_context: Any,
    ) -> RuntimeResult:
        try:
            return cast(RuntimeResult, await runtime.execute(runtime_context))
        except RuntimeException as exc:
            return RuntimeResult(
                workflow=runtime_context.workflow,
                execution=runtime_context.execution,
                status="failed",
                error=exc,
                retryable=exc.retryable,
                retry_after_seconds=runtime_context.retry_delay_seconds,
                metadata=dict(exc.metadata),
            )
        except Exception as exc:  # pragma: no cover - defensive
            return RuntimeResult(
                workflow=runtime_context.workflow,
                execution=runtime_context.execution,
                status="failed",
                error=RuntimeException(
                    "Workflow runtime failed",
                    runtime_name=runtime_context.execution.runtime_name,
                    workflow_id=runtime_context.workflow.workflow_id,
                    execution_id=runtime_context.execution.execution_id,
                    attempt=runtime_context.attempt,
                    retryable=False,
                    metadata={"exception_type": type(exc).__name__},
                ),
                retryable=False,
            )

    def _maybe_update_checkpoint(
        self,
        state: WorkflowState,
        runtime_result: RuntimeResult,
    ) -> WorkflowState:
        checkpoint_id = runtime_result.metadata.get("checkpoint_id")
        if checkpoint_id is None:
            return state
        return state.with_checkpoint(str(checkpoint_id))

    async def _emit(
        self,
        context: WorkflowContext,
        event_name: str,
        payload: dict[str, Any],
    ) -> None:
        emitter = context.event_emitter
        if emitter is None:
            return
        try:
            await emitter.emit(event_name, payload, context)
        except Exception:
            return
