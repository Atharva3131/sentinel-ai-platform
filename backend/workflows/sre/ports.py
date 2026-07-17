"""Protocol ports for SRE workflow external dependencies."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from backend.agents.context import AgentContext
from backend.agents.models import AgentExecutionResult, AgentInput
from backend.retrieval.context import RetrievalContext
from backend.retrieval.models import RetrievalMetadata, RetrievalResult
from backend.workflows.sre.models import (
    ApprovalOutcome,
    ApprovalRequest,
    AuditEntry,
    IncidentContext,
    RecoveryAction,
    RecoveryResult,
)


@runtime_checkable
class AgentRunnerPort(Protocol):
    """Port that matches ``AgentRunner.execute()`` structurally.

    Allows SRE phase implementations to accept the real ``AgentRunner`` or
    test fakes without coupling to the concrete class.
    """

    async def execute(
        self,
        name: str,
        *,
        context: AgentContext,
        agent_input: AgentInput,
        version: str | None = None,
        dependencies: dict[str, Any] | None = None,
    ) -> AgentExecutionResult:
        ...


@runtime_checkable
class KnowledgeRetrieverPort(Protocol):
    """Port for knowledge-graph + vector retrieval.

    The real ``KnowledgeManager.retrieve()`` satisfies this structurally.
    """

    async def retrieve(
        self,
        context: RetrievalContext,
        *,
        pipeline: Any = None,
    ) -> tuple[list[RetrievalResult], RetrievalMetadata]:
        ...


@runtime_checkable
class ApprovalGateway(Protocol):
    """Port for human-approval integration.

    Implementations may delegate to PagerDuty, Slack, an internal portal,
    or a stub that auto-approves for fully-autonomous deployments.
    """

    async def request_approval(
        self,
        request: ApprovalRequest,
    ) -> ApprovalOutcome:
        ...


@runtime_checkable
class AuditRepository(Protocol):
    """Port for durable audit-log persistence.

    Production implementations write to Cosmos DB or PostgreSQL.
    Failures are surfaced as ``AuditError`` by ``WorkflowAuditor``.
    """

    async def persist(self, entry: AuditEntry) -> None:
        ...

    async def list_entries(self, incident_id: str) -> list[AuditEntry]:
        ...


@runtime_checkable
class RecoveryExecutor(Protocol):
    """Port for executing remediation actions against infrastructure.

    Implementations are responsible for idempotency guarantees; the
    ``RecoveryCoordinator`` does not retry individual actions.
    """

    async def execute_actions(
        self,
        incident: IncidentContext,
        actions: list[RecoveryAction],
        *,
        correlation_id: str | None = None,
    ) -> RecoveryResult:
        ...
