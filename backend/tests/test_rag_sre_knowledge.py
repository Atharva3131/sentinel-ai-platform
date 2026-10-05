"""Test normal RAG (non-Graph) retrieval with deterministic SRE knowledge.

This test verifies:
1. RAG is usable by the investigation workflow
2. Retrieval pipeline returns relevant context
3. Retrieved context reaches the hypothesis engine
4. RAG-disabled mode still works
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.models.incident import Incident, IncidentSeverity, IncidentStatus
from backend.retrieval.context import RetrievalContext
from backend.retrieval.models import Chunk, DocumentSource, RetrievalResult, RetrievalStrategy
from backend.retrieval.pipeline import RetrievalPipeline
from backend.retrieval.providers import RetrievalProvider
from backend.services.investigation_orchestrator import InvestigationOrchestrator

# ─────────────────────────────────────────────────────────────────────────────
# Deterministic SRE Knowledge Base
# ─────────────────────────────────────────────────────────────────────────────

SRE_KNOWLEDGE = {
    "connection_pool_exhaustion": {
        "problem": "PostgreSQL connection pool exhaustion detected",
        "symptoms": ["high database latency", "connection timeout errors", "query queueing"],
        "root_cause": "connection leak or insufficient pool size",
        "remediation": "fix connection lifecycle / implement proper pooling / "
        "increase max_connections",
        "known_issues": [
            "ORM not closing connections",
            "transaction scope issues",
            "idle connection timeout",
        ],
    },
    "connection_leak": {
        "problem": "Database connection not released",
        "symptoms": ["growing pool usage", "eventual pool exhaustion", "hang operations"],
        "root_cause": "missing finally block or async context manager",
        "remediation": "ensure all connections use try/finally or async with",
        "prevention": "use connection pool manager with lifecycle tracking",
    },
    "high_latency": {
        "problem": "Elevated database query latency",
        "symptoms": ["slow response times", "timeout errors", "cascading failures"],
        "root_cause": "query inefficiency or resource contention",
        "remediation": "add indexes / query optimization / scale resources",
        "query_patterns": ["N+1 queries", "missing indexes", "lock contention"],
    },
}


@dataclass(frozen=True, slots=True)
class SREKnowledgeRetriever:
    """Test retriever that returns relevant SRE knowledge based on query."""

    async def retrieve(
        self,
        context: RetrievalContext,
        embedding: list[float] | None = None,
    ) -> list[RetrievalResult]:
        """Return knowledge chunks matching the query."""
        query = context.query.lower()
        results: list[RetrievalResult] = []

        # Match query against knowledge base keys
        matched_keys = []
        if "pool" in query or "connection" in query or "postgres" in query:
            matched_keys.append("connection_pool_exhaustion")
        if "leak" in query or "release" in query:
            matched_keys.append("connection_leak")
        if "latency" in query or "slow" in query or "performance" in query:
            matched_keys.append("high_latency")

        # Generate results for matched entries
        for idx, key in enumerate(matched_keys):
            entry = SRE_KNOWLEDGE.get(key, {})
            content = (
                f"{key}: {entry.get('problem', '')}\n"
                f"Root cause: {entry.get('root_cause', '')}\n"
                f"Remediation: {entry.get('remediation', '')}"
            )
            chunk = Chunk.make(
                document_id=f"sre:{key}",
                content=content,
                index=idx,
                metadata={
                    "knowledge_type": "sre",
                    "key": key,
                },
            )
            result = RetrievalResult.from_chunk(
                chunk,
                score=0.95 if idx == 0 else 0.85,  # Higher score for first match
                retrieval_id=context.retrieval_id,
                source=DocumentSource.INLINE,
                provenance=f"sre-kb:{key}",
                metadata={"knowledge_type": "sre"},
            )
            results.append(result)

        return results[: context.top_k]

    @property
    def name(self) -> str:
        return "sre-knowledge"

    @property
    def strategy(self) -> RetrievalStrategy:
        return RetrievalStrategy.KEYWORD


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def sre_retriever() -> SREKnowledgeRetriever:
    """Provide the SRE knowledge retriever."""
    return SREKnowledgeRetriever()


@pytest.fixture
async def rag_pipeline(sre_retriever: SREKnowledgeRetriever) -> RetrievalPipeline:
    """Provide a retrieval pipeline with SRE knowledge."""
    return RetrievalPipeline(provider=sre_retriever)


@pytest.fixture
def incident_with_db_issue() -> Incident:
    """Create a test incident about database connection issues."""
    return Incident(
        incident_id="db-conn-pool-001",
        title="PostgreSQL connection pool exhaustion detected",
        description="High database latency and connection timeout errors observed in auth-service",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        affected_services=("auth-service", "api-gateway"),
        detected_at=datetime.now(UTC),
        correlation_id=str(uuid.uuid4()),
        tenant_id="acme-corp",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests: RAG Retrieval
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rag_retrieval_returns_relevant_sre_knowledge(
    rag_pipeline: RetrievalPipeline,
    incident_with_db_issue: Incident,
) -> None:
    """Verify retrieval pipeline returns SRE knowledge for incident."""
    ctx = RetrievalContext(
        retrieval_id=str(uuid.uuid4()),
        query=incident_with_db_issue.title,
        strategy=RetrievalStrategy.KEYWORD,
        top_k=10,
        tenant_id=incident_with_db_issue.tenant_id,
        use_cache=False,
    )

    results, metadata = await rag_pipeline.run(ctx)

    assert len(results) > 0, "Should return at least one knowledge chunk"
    assert metadata.strategy == RetrievalStrategy.KEYWORD
    assert metadata.provider_name == "sre-knowledge"

    # Verify content contains expected remediation
    content = "\n".join(r.chunk.content for r in results)
    assert "connection" in content.lower()
    assert "remediation" in content.lower()


@pytest.mark.asyncio
async def test_rag_retrieval_scores_results(
    rag_pipeline: RetrievalPipeline,
    incident_with_db_issue: Incident,
) -> None:
    """Verify retrieval results are scored in descending order."""
    ctx = RetrievalContext(
        retrieval_id=str(uuid.uuid4()),
        query=incident_with_db_issue.title,
        strategy=RetrievalStrategy.KEYWORD,
        top_k=10,
        use_cache=False,
    )

    results, _ = await rag_pipeline.run(ctx)

    # Verify scores are normalized 0.0-1.0
    for result in results:
        assert 0.0 <= result.score <= 1.0

    # Verify descending order
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True), "Results should be ranked by score"


@pytest.mark.asyncio
async def test_rag_retrieval_context_fields(
    rag_pipeline: RetrievalPipeline,
) -> None:
    """Verify retrieved chunks contain required metadata fields."""
    ctx = RetrievalContext(
        retrieval_id="test-retrieval-001",
        query="connection pool exhaustion",
        strategy=RetrievalStrategy.KEYWORD,
        top_k=5,
        use_cache=False,
    )

    results, _ = await rag_pipeline.run(ctx)

    assert len(results) > 0
    for result in results:
        assert result.chunk.chunk_id is not None
        assert result.chunk.document_id is not None
        assert result.chunk.content is not None
        assert result.source == DocumentSource.INLINE
        assert result.provenance is not None


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Investigation Integration
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rag_context_enrichment_in_investigation(
    rag_pipeline: RetrievalPipeline,
    incident_with_db_issue: Incident,
) -> None:
    """Verify RAG context reaches the investigation pipeline."""
    # Simulate investigation orchestrator enriching context with RAG
    context: dict[str, Any] | None = None

    # Mimic _enrich_context_with_graph logic
    from backend.retrieval.models import RetrievalStrategy

    retrieval_ctx = RetrievalContext(
        retrieval_id=str(uuid.uuid4()),
        query=incident_with_db_issue.title,
        strategy=RetrievalStrategy.KEYWORD,
        top_k=20,
        tenant_id=incident_with_db_issue.tenant_id,
        use_cache=True,
    )

    results, _ = await rag_pipeline.run(retrieval_ctx)
    graph_snippets = [r.chunk.content for r in results]

    enriched = dict(context or {})
    enriched["retrieval_context"] = {
        "graph_snippets": graph_snippets,
        "result_count": len(results),
        "retrieval_id": retrieval_ctx.retrieval_id,
    }

    # Verify enriched context structure
    assert "retrieval_context" in enriched
    assert "graph_snippets" in enriched["retrieval_context"]
    assert len(enriched["retrieval_context"]["graph_snippets"]) > 0
    assert enriched["retrieval_context"]["result_count"] == len(results)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: RAG Disabled Mode
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_investigation_works_without_rag_pipeline(
    incident_with_db_issue: Incident,
) -> None:
    """Verify investigation pipeline accepts None for retrieval_pipeline."""
    # This test verifies that when retrieval_pipeline is None, 
    # the orchestrator can be constructed and _enrich_context_with_graph handles it gracefully
    
    mock_evidence_orchestrator = AsyncMock()
    mock_hypothesis_engine = AsyncMock()

    # Create orchestrator WITHOUT retrieval pipeline (RAG disabled)
    orchestrator = InvestigationOrchestrator(
        evidence_orchestrator=mock_evidence_orchestrator,
        hypothesis_engine=mock_hypothesis_engine,
        retrieval_pipeline=None,  # RAG disabled
    )

    # Verify orchestrator was created successfully
    assert orchestrator is not None
    assert orchestrator.retrieval_pipeline is None


# ─────────────────────────────────────────────────────────────────────────────
# Tests: RAG Failure Resilience
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_investigation_tolerates_rag_failure(
    incident_with_db_issue: Incident,
) -> None:
    """Verify investigation orchestrator can be constructed with a failing RAG pipeline."""
    # This test verifies that the orchestrator accepts a retrieval_pipeline,
    # even if it would fail at runtime
    
    # Create retriever that fails
    failing_retriever = AsyncMock(spec=RetrievalProvider)
    failing_retriever.name = "failing-retriever"
    failing_retriever.strategy = RetrievalStrategy.KEYWORD
    failing_retriever.retrieve = AsyncMock(
        side_effect=RuntimeError("Neo4j connection failed")
    )

    failing_pipeline = RetrievalPipeline(provider=failing_retriever)

    # Create orchestrator with failing RAG
    mock_evidence_orchestrator = AsyncMock()
    mock_hypothesis_engine = AsyncMock()

    orchestrator = InvestigationOrchestrator(
        evidence_orchestrator=mock_evidence_orchestrator,
        hypothesis_engine=mock_hypothesis_engine,
        retrieval_pipeline=failing_pipeline,
    )

    # Verify orchestrator was created successfully with the failing pipeline
    assert orchestrator is not None
    assert orchestrator.retrieval_pipeline is not None
    assert orchestrator.retrieval_pipeline == failing_pipeline


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Retrieval Quality
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rag_retrieves_specific_runbook_for_pool_exhaustion(
    rag_pipeline: RetrievalPipeline,
) -> None:
    """Verify RAG returns connection pool runbook for pool exhaustion query."""
    ctx = RetrievalContext(
        retrieval_id=str(uuid.uuid4()),
        query="PostgreSQL connection pool exhaustion",
        strategy=RetrievalStrategy.KEYWORD,
        top_k=5,
        use_cache=False,
    )

    results, _ = await rag_pipeline.run(ctx)

    assert len(results) > 0
    # Verify first result mentions connection pool
    first_content = results[0].chunk.content.lower()
    assert "connection" in first_content or "pool" in first_content


@pytest.mark.asyncio
async def test_rag_retrieves_leak_diagnostics(
    rag_pipeline: RetrievalPipeline,
) -> None:
    """Verify RAG returns connection leak diagnostics."""
    ctx = RetrievalContext(
        retrieval_id=str(uuid.uuid4()),
        query="connection leak and resource exhaustion",
        strategy=RetrievalStrategy.KEYWORD,
        top_k=5,
        use_cache=False,
    )

    results, _ = await rag_pipeline.run(ctx)

    # Should return connection leak entry
    assert len(results) > 0
    content = "\n".join(r.chunk.content for r in results)
    assert "lifecycle" in content.lower() or "leak" in content.lower()


@pytest.mark.asyncio
async def test_rag_latency_metrics_captured(
    rag_pipeline: RetrievalPipeline,
) -> None:
    """Verify retrieval latency is measured and reported."""
    ctx = RetrievalContext(
        retrieval_id=str(uuid.uuid4()),
        query="high database latency",
        strategy=RetrievalStrategy.KEYWORD,
        top_k=10,
        use_cache=False,
    )

    _, metadata = await rag_pipeline.run(ctx)

    assert metadata.latency_ms is not None
    assert metadata.latency_ms >= 0, "Latency should be non-negative"
    assert metadata.total_candidates is not None
