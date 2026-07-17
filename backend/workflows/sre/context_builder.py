"""IncidentContextBuilder — retrieves knowledge-graph context for an incident."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from backend.retrieval.context import RetrievalContext
from backend.retrieval.models import RetrievalMetadata, RetrievalResult, RetrievalStrategy
from backend.workflows.sre.exceptions import SREPhaseError
from backend.workflows.sre.models import IncidentContext
from backend.workflows.sre.ports import KnowledgeRetrieverPort

_GRAPH_NODE_TYPES: frozenset[str] = frozenset(
    {"Service", "Symptom", "Runbook", "Incident", "Component"}
)
_GRAPH_RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {"DEPENDS_ON", "HAS_RUNBOOK", "AFFECTED", "OBSERVED"}
)


@dataclass(slots=True)
class IncidentContextBuilder:
    """Retrieves multi-modal context from the Knowledge Engine for an incident.

    Builds a ``RetrievalContext`` from the incident fields, executes a hybrid
    (vector + graph) retrieval, and returns the ranked results alongside
    retrieval metadata for use by downstream phases.

    All retrieval errors are re-raised as ``SREPhaseError`` with
    ``phase='context_retrieval'`` so the orchestrator can emit structured
    failure events.
    """

    retriever: KnowledgeRetrieverPort
    max_results: int = 20
    graph_depth: int = 2
    cache_ttl_seconds: float = 300.0

    async def build(
        self,
        incident: IncidentContext,
        *,
        workflow_id: str | None = None,
        execution_id: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[list[RetrievalResult], RetrievalMetadata]:
        """Retrieve relevant context for *incident*.

        Returns:
            Tuple of (ranked results, retrieval metadata).

        Raises:
            SREPhaseError: If the retriever raises any exception.
        """
        retrieval_id = str(uuid.uuid4())
        query = self._build_query(incident)
        context = RetrievalContext(
            retrieval_id=retrieval_id,
            query=query,
            strategy=RetrievalStrategy.HYBRID,
            top_k=self.max_results,
            workflow_id=workflow_id,
            execution_id=execution_id,
            correlation_id=correlation_id,
            tenant_id=incident.tenant_id,
            graph_depth=self.graph_depth,
            graph_node_types=_GRAPH_NODE_TYPES,
            graph_relationship_types=_GRAPH_RELATIONSHIP_TYPES,
            cache_ttl_seconds=self.cache_ttl_seconds,
            filters={
                "services": list(incident.affected_services),
                "severity": str(incident.severity),
            },
        )
        try:
            return await self.retriever.retrieve(context)
        except Exception as exc:
            raise SREPhaseError(
                f"Knowledge retrieval failed for incident '{incident.incident_id}': {exc}",
                phase="context_retrieval",
                incident_id=incident.incident_id,
                retryable=True,
            ) from exc

    def _build_query(self, incident: IncidentContext) -> str:
        parts: list[str] = [incident.title]
        if incident.symptoms:
            parts.extend(incident.symptoms[:3])
        if incident.affected_services:
            parts.extend(incident.affected_services[:5])
        if incident.description:
            parts.append(incident.description[:200])
        return " ".join(parts)
