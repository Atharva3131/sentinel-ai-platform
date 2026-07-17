"""SREWorkflowRuntime — WorkflowRuntime implementation for the AI SRE workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.runtime.context import RuntimeContext, WorkflowMetadata
from backend.runtime.exceptions import RuntimeException
from backend.runtime.results import RuntimeResult
from backend.workflows.sre.exceptions import SREWorkflowError
from backend.workflows.sre.models import IncidentContext
from backend.workflows.sre.orchestrator import SREWorkflowOrchestrator

_RUNTIME_NAME = "sre-workflow"
_RUNTIME_VERSION = "1.0.0"
_CAPABILITIES: tuple[str, ...] = ("incident_response", "sre", "autonomous_recovery")

_INCIDENT_KEY = "incident"


@dataclass(slots=True)
class SREWorkflowRuntime:
    """WorkflowRuntime implementation that drives the AI SRE incident response.

    The runtime extracts an ``IncidentContext`` from
    ``RuntimeContext.metadata["incident"]`` and delegates the full lifecycle to
    ``SREWorkflowOrchestrator``.

    The runtime satisfies the ``WorkflowRuntime`` Protocol structurally.  It is
    registered under the name ``"sre-workflow"`` and responds to any workflow
    whose *capabilities* intersect with ``("incident_response", "sre")``.
    """

    orchestrator: SREWorkflowOrchestrator

    @property
    def name(self) -> str:
        return _RUNTIME_NAME

    @property
    def version(self) -> str | None:
        return _RUNTIME_VERSION

    @property
    def capabilities(self) -> tuple[str, ...]:
        return _CAPABILITIES

    def supports(self, workflow: WorkflowMetadata) -> bool:
        """Return True when the workflow declares any SRE capability."""
        return bool(set(workflow.capabilities) & {"incident_response", "sre"})

    async def execute(self, context: RuntimeContext) -> RuntimeResult:
        """Run the SRE workflow for the incident in *context.metadata*.

        The caller must supply a serialised or live ``IncidentContext`` in
        ``context.metadata["incident"]``.  If the key is absent or has the
        wrong type, the runtime returns a failed ``RuntimeResult`` immediately.
        """
        incident = _extract_incident(context)
        if incident is None:
            return _error_result(
                context,
                "IncidentContext missing from context.metadata['incident']",
                retryable=False,
            )

        try:
            result = await self.orchestrator.run(incident, context)
        except SREWorkflowError as exc:
            return _error_result(context, str(exc), retryable=False)
        except Exception as exc:  # noqa: BLE001
            return _error_result(context, f"Unexpected error: {exc}", retryable=True)

        terminal_status = "completed" if result.status in ("completed", "mitigated") else "failed"
        return RuntimeResult(
            workflow=context.workflow,
            execution=context.execution,
            status=terminal_status,
            output=result,
            metadata={"sre_status": result.status},
        )

    async def cancel(self, context: RuntimeContext) -> None:
        """Signal cancellation via the context's cancellation token.

        The orchestrator polls ``context.is_cancelled()`` between phases, so
        this is a best-effort cooperative mechanism.
        """
        if context.cancellation_token is not None:
            # The token interface only exposes is_set/wait; cancellation is set
            # externally by the caller that owns the token object.
            pass


def _extract_incident(context: RuntimeContext) -> IncidentContext | None:
    raw: Any = context.metadata.get(_INCIDENT_KEY)
    if isinstance(raw, IncidentContext):
        return raw
    return None


def _error_result(
    context: RuntimeContext,
    message: str,
    *,
    retryable: bool,
) -> RuntimeResult:
    return RuntimeResult(
        workflow=context.workflow,
        execution=context.execution,
        status="failed",
        error=RuntimeException(message),
        retryable=retryable,
    )
