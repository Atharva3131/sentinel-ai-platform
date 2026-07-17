"""SRE workflow definition and registry bootstrap helpers."""

from __future__ import annotations

from backend.runtime.registry import RuntimeRegistry
from backend.workflows.definition import WorkflowDefinition
from backend.workflows.lifecycle import WorkflowLifecycle
from backend.workflows.policy import ExecutionPolicy
from backend.workflows.registry import WorkflowRegistry
from backend.workflows.sre.orchestrator import SREWorkflowOrchestrator
from backend.workflows.sre.runtime import SREWorkflowRuntime

_WORKFLOW_ID = "sre-incident-response"
_WORKFLOW_NAME = "AI SRE Incident Response"
_WORKFLOW_VERSION = "1.0.0"
_RUNTIME_NAME = "sre-workflow"


def sre_workflow_definition(
    *,
    timeout_seconds: float = 600.0,
    max_attempts: int = 2,
) -> WorkflowDefinition:
    """Return the canonical SRE workflow definition.

    Registers with capabilities ``("incident_response", "sre")`` so that
    ``SREWorkflowRuntime.supports()`` matches it automatically.
    """
    return WorkflowDefinition(
        workflow_id=_WORKFLOW_ID,
        name=_WORKFLOW_NAME,
        version=_WORKFLOW_VERSION,
        runtime_name=_RUNTIME_NAME,
        description=(
            "Autonomous AI SRE workflow: context retrieval → plan build → "
            "analysis → confidence evaluation → approval gate → recovery."
        ),
        capabilities=("incident_response", "sre", "autonomous_recovery"),
        labels=("sre", "production", "flagship"),
        execution_policy=ExecutionPolicy(
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        ),
        lifecycle=WorkflowLifecycle(),
    )


def register_sre_workflow(
    workflow_registry: WorkflowRegistry,
    runtime_registry: RuntimeRegistry,
    orchestrator: SREWorkflowOrchestrator,
    *,
    override: bool = False,
    default_runtime: bool = False,
) -> SREWorkflowRuntime:
    """Register the SRE workflow definition and its runtime in one call.

    Returns the ``SREWorkflowRuntime`` instance for downstream use (e.g.
    DI container wiring, testing).

    Args:
        workflow_registry: The application-level workflow definition registry.
        runtime_registry:  The runtime resolution registry.
        orchestrator:      Fully-wired orchestrator with all phase services.
        override:          When True, overwrite any existing registration with
                           the same name instead of raising.
        default_runtime:   When True, set the SRE runtime as the default.
    """
    definition = sre_workflow_definition()
    workflow_registry.register(definition, override=override)

    sre_runtime = SREWorkflowRuntime(orchestrator=orchestrator)
    runtime_registry.register(
        _RUNTIME_NAME,
        sre_runtime,
        default=default_runtime,
        override=override,
    )

    return sre_runtime
