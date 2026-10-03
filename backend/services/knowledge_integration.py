"""KnowledgeEngineIntegration — connects the Knowledge/Retrieval Engine to investigation.

The Knowledge Engine (GraphRAG + vector retrieval) is an optional enrichment
source.  When knowledge is available, it is surfaced as ``Evidence`` items of
kind ``KNOWLEDGE_BASE`` and ``HISTORICAL_INCIDENTS``, consumed alongside all
other evidence by the HypothesisEngine.

Design:
  * KnowledgeEvidenceProvider wraps the KnowledgeRetrieverPort from the SRE
    workflow ports so no new interface is introduced.
  * When retrieval returns no results, an empty list is returned — the
    investigation continues with whatever other evidence is present.
  * GraphRAG is NOT mandatory; the system degrades gracefully.
  * No AWS/Azure/GitHub imports.

Usage:
    provider = KnowledgeEvidenceProvider(retriever=my_retriever)
    evidence_orchestrator.register(provider)
    # From this point forward, knowledge results appear automatically as
    # EvidenceSourceKind.KNOWLEDGE_BASE items in every collection.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from backend.models.evidence import (
    Evidence,
    EvidenceSource,
    EvidenceSourceKind,
    EvidenceStatus,
)
from backend.models.incident import Incident
from backend.retrieval.context import RetrievalContext
from backend.retrieval.models import RetrievalResult, RetrievalStrategy
from backend.workflows.sre.ports import KnowledgeRetrieverPort

log = structlog.get_logger(__name__)

_DEFAULT_MAX_RESULTS = 10
_DEFAULT_MIN_SCORE = 0.3


# ---------------------------------------------------------------------------
# KnowledgeEvidenceProvider
# ---------------------------------------------------------------------------


@dataclass
class KnowledgeEvidenceProvider:
    """EvidenceProvider that wraps the KnowledgeRetrieverPort.

    Satisfies the structural ``EvidenceProvider`` protocol so it can be
    registered with ``EvidenceOrchestrator`` or ``EvidenceCollector`` directly.

    The kind is ``KNOWLEDGE_BASE``; historical-incident results from GraphRAG
    are surfaced as the same kind (the content makes provenance clear).

    ``retriever``       — KnowledgeRetrieverPort implementation
    ``provider_name``   — stable identifier for logging and provenance
    ``max_results``     — upper bound on knowledge items returned
    ``min_score``       — minimum retrieval score to include a result
    ``retrieval_strategy`` — passed to RetrievalContext (default: HYBRID)
    """

    retriever: KnowledgeRetrieverPort
    provider_name: str = "knowledge-base"
    max_results: int = _DEFAULT_MAX_RESULTS
    min_score: float = _DEFAULT_MIN_SCORE
    retrieval_strategy: RetrievalStrategy = RetrievalStrategy.HYBRID

    @property
    def kind(self) -> EvidenceSourceKind:
        return EvidenceSourceKind.KNOWLEDGE_BASE

    @property
    def name(self) -> str:
        return self.provider_name

    async def collect(
        self,
        incident: Incident,
        *,
        max_items: int = 20,
        context: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        """Retrieve knowledge relevant to *incident* and return as Evidence items."""
        bound_log = log.bind(
            incident_id=incident.incident_id,
            provider=self.provider_name,
        )

        # Build a retrieval query from the incident
        query = _build_query(incident)
        retrieval_context = RetrievalContext(
            retrieval_id=str(uuid.uuid4()),
            query=query,
            strategy=self.retrieval_strategy,
            workflow_id=context.get("workflow_id") if context else None,
            execution_id=context.get("execution_id") if context else None,
            correlation_id=incident.correlation_id,
            tenant_id=incident.tenant_id,
            top_k=min(max_items, self.max_results),
            min_score=self.min_score,
            metadata={
                "incident_id": incident.incident_id,
                "severity": str(incident.severity),
                "affected_services": list(incident.affected_services),
            },
        )

        try:
            results, ret_metadata = await self.retriever.retrieve(retrieval_context)
        except Exception as exc:
            bound_log.warning(
                "knowledge_retrieval_failed",
                error=str(exc),
            )
            return []

        if not results:
            bound_log.debug("knowledge_retrieval_empty")
            return []

        bound_log.info(
            "knowledge_retrieved",
            result_count=len(results),
            latency_ms=ret_metadata.latency_ms,
            cached=ret_metadata.cached,
        )

        return [
            _result_to_evidence(r, incident, self.provider_name)
            for r in results
            if r.score >= self.min_score
        ][:max_items]

    async def health_check(self) -> bool:
        """Return True; the retriever is considered available if it can be called."""
        try:
            ctx = RetrievalContext(
                retrieval_id=str(uuid.uuid4()),
                query="health check",
                strategy=self.retrieval_strategy,
                min_score=1.0,  # unreachable threshold — returns nothing
                top_k=1,
            )
            await self.retriever.retrieve(ctx)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_query(incident: Incident) -> str:
    """Construct a retrieval query from incident fields."""
    parts = [incident.title]
    if incident.affected_services:
        parts.append(f"services: {', '.join(incident.affected_services[:5])}")
    if incident.symptoms:
        parts.append(f"symptoms: {'; '.join(incident.symptoms[:3])}")
    if incident.description:
        parts.append(incident.description[:200])
    return " | ".join(parts)


def _result_to_evidence(
    result: RetrievalResult,
    incident: Incident,
    provider_name: str,
) -> Evidence:
    """Convert one RetrievalResult into an Evidence item."""
    now = datetime.now(UTC)

    # Extract chunk content
    content = result.chunk.content[:1000] if result.chunk.content else "(no content)"

    # Title from the chunk's document metadata, or the document_id
    title = (
        result.chunk.metadata.get("title")
        if result.chunk.metadata
        else None
    ) or f"Knowledge: {result.chunk.document_id}"

    # Map retrieval score → relevance (already 0-1 from retrieval engine)
    relevance = min(1.0, max(0.0, result.score))

    source = EvidenceSource(
        source_id=str(uuid.uuid4()),
        kind=EvidenceSourceKind.KNOWLEDGE_BASE,
        name=provider_name,
        collected_at=now,
        authoritative=False,      # knowledge base is supplementary, not authoritative
        metadata={
            "document_id": result.chunk.document_id,
            "chunk_id": result.chunk.chunk_id,
            "retrieval_score": result.score,
        },
    )

    return Evidence(
        evidence_id=str(uuid.uuid4()),
        incident_id=incident.incident_id,
        source=source,
        title=str(title),
        content=content,
        status=EvidenceStatus.PROBABLE,   # retrieved knowledge is probable, not confirmed
        relevance_score=relevance,
        collected_at=now,
        structured_data={
            "document_id": result.chunk.document_id,
            "chunk_id": result.chunk.chunk_id,
        },
        tags=("knowledge_base",),
    )
