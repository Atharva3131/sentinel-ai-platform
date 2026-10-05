"""InvestigationOrchestrator — the iterative evidence→RCA pipeline.

Pipeline:
    EvidenceOrchestrator
            ↓
    EvidenceCollection  (+ NormalizedEvidenceCollection)
            ↓
    HypothesisEngine.analyze()
            ↓
    RootCauseAnalysis
      ├── confidence >= threshold  →  done
      └── confidence < threshold   →  request additional evidence
                                       ↓
                                   (next iteration)
                                       ↓
                                   re-evaluate hypotheses
                                       ↓  (up to max_iterations)
                                   RCA (possibly inconclusive)

Responsibilities:
  * Manage the iterative loop: request more evidence when RCA is inconclusive.
  * Emit investigation lifecycle events at each phase and iteration.
  * Propagate incident_id / correlation_id / execution_id through every event.
  * Respect cancellation and per-iteration timeouts.
  * Tolerate partial evidence — do not require all providers to succeed.
  * Return a rich InvestigationResult regardless of whether a root cause was found.

The orchestrator does NOT:
  * Know about specific failure modes.
  * Branch on incident type.
  * Access provider SDKs directly.
  * Import cloud-specific libraries.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from backend.core.hypothesis_engine import HypothesisEngine
from backend.models.evidence import (
    Evidence,
    EvidenceCollection,
    EvidenceCorrelation,
    EvidenceSourceKind,
)
from backend.models.hypothesis import RootCauseAnalysis
from backend.models.incident import Incident
from backend.retrieval.pipeline import RetrievalPipeline
from backend.services.evidence_normalizer import NormalizedEvidenceCollection
from backend.services.evidence_orchestrator import (
    EvidenceOrchestrationResult,
    EvidenceOrchestrator,
    IncidentEventEmitter,
)
from backend.services.incident_events import (
    HYPOTHESES_GENERATED,
    INVESTIGATION_ITERATION_COMPLETED,
    INVESTIGATION_ITERATION_STARTED,
    INVESTIGATION_STARTED,
    RCA_INCONCLUSIVE,
    ROOT_CAUSE_IDENTIFIED,
)

log = structlog.get_logger(__name__)

_DEFAULT_MAX_ITERATIONS = 3
_DEFAULT_CONFIDENCE_THRESHOLD = 0.60
_DEFAULT_ITERATION_TIMEOUT_SECONDS = 120.0


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


class AdditionalEvidenceSelectorProtocol:
    """Protocol: decides which evidence categories to request next.

    Default implementation requests all categories not yet collected.
    Production code can wire an LLM-backed selector that reasons over the
    current NormalizedEvidenceCollection to pick the most promising kinds.
    """

    def select(
        self,
        rca: RootCauseAnalysis,
        normalized: NormalizedEvidenceCollection,
        already_requested: set[EvidenceSourceKind],
    ) -> set[EvidenceSourceKind]:
        """Return evidence kinds to request in the next iteration."""
        all_kinds = set(EvidenceSourceKind)
        # Exclude kinds already collected and synthetic/other
        exclude = already_requested | {
            EvidenceSourceKind.SYNTHETIC,
            EvidenceSourceKind.OTHER,
        }
        return all_kinds - exclude


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvestigationResult:
    """Complete output of the investigation orchestration pipeline.

    ``rca``              — final RootCauseAnalysis (always present)
    ``evidence_results`` — one EvidenceOrchestrationResult per iteration
    ``iterations``       — how many evidence-gather + analyze cycles ran
    ``inconclusive``     — True when no hypothesis exceeded the threshold
    ``cancelled``        — True when a cancellation token fired
    ``timed_out``        — True when the total budget was exceeded
    ``duration_ms``      — total wall-clock time for the full investigation
    ``incident_id``      — for convenient look-up
    ``execution_id``     — propagated from the caller
    """

    rca: RootCauseAnalysis
    evidence_results: tuple[EvidenceOrchestrationResult, ...]
    iterations: int
    inconclusive: bool
    cancelled: bool
    timed_out: bool
    duration_ms: float
    incident_id: str
    execution_id: str | None = None

    @property
    def all_evidence(self) -> EvidenceCollection:
        """Merge all per-iteration collections into one for convenience."""
        if not self.evidence_results:
            # Return an empty collection — callers must handle this
            return EvidenceCollection(
                incident_id=self.incident_id,
                items=(),
                correlations=(),
                collected_at=datetime.now(UTC),
            )
        # Last iteration's collection is the most complete
        return self.evidence_results[-1].collection

    @property
    def provider_errors(self) -> dict[str, str]:
        """Union of all provider errors across iterations."""
        merged: dict[str, str] = {}
        for r in self.evidence_results:
            merged.update(r.provider_errors)
        return merged


# ---------------------------------------------------------------------------
# InvestigationOrchestrator
# ---------------------------------------------------------------------------


@dataclass
class InvestigationOrchestrator:
    """Iterative evidence → hypothesis → RCA pipeline.

    Inject:
      ``evidence_orchestrator``  — EvidenceOrchestrator (providers registered externally)
      ``hypothesis_engine``      — HypothesisEngine with a HypothesisGenerator
      ``event_emitter``          — IncidentEventEmitter (optional)
      ``additional_selector``    — AdditionalEvidenceSelectorProtocol (optional)
      ``retrieval_pipeline``     — RetrievalPipeline for GraphRAG context (optional).
                                   When set, a graph traversal is run once before the
                                   evidence loop and results are injected into the
                                   ``context`` dict under the key ``"retrieval_context"``.
                                   Guarded by the ``GRAPH_RAG`` feature flag in the DI
                                   layer; the orchestrator itself is flag-agnostic.
      ``max_iterations``         — loop ceiling (default 3)
      ``confidence_threshold``   — minimum RCA confidence to stop iterating
      ``iteration_timeout_seconds`` — per-iteration wall-clock budget
    """

    evidence_orchestrator: EvidenceOrchestrator
    hypothesis_engine: HypothesisEngine
    event_emitter: IncidentEventEmitter | None = None
    additional_selector: AdditionalEvidenceSelectorProtocol = field(
        default_factory=AdditionalEvidenceSelectorProtocol
    )
    retrieval_pipeline: RetrievalPipeline | None = None
    max_iterations: int = _DEFAULT_MAX_ITERATIONS
    confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD
    iteration_timeout_seconds: float = _DEFAULT_ITERATION_TIMEOUT_SECONDS

    async def investigate(
        self,
        incident: Incident,
        *,
        execution_id: str | None = None,
        context: dict[str, Any] | None = None,
        cancellation_check: Any = None,
        emitter_context: Any = None,
    ) -> InvestigationResult:
        """Run the full iterative investigation for *incident*.

        Args:
            incident:          The incident to investigate.
            execution_id:      Propagated into events and the result.
            context:           Forwarded to every provider and engine call.
            cancellation_check: Optional callable ``() -> bool``; if it returns
                                True the investigation is cancelled gracefully.
            emitter_context:   Passed to event_emitter.emit().

        Returns:
            InvestigationResult — always returned, never raises for provider
            or RCA failures.  Cancellation and timeout are signalled via
            ``cancelled`` and ``timed_out`` flags.
        """
        total_start = time.monotonic()
        execution_id = execution_id or str(uuid.uuid4())
        bound_log = log.bind(
            incident_id=incident.incident_id,
            execution_id=execution_id,
            correlation_id=incident.correlation_id,
        )

        await self._emit(
            INVESTIGATION_STARTED,
            {
                "incident_id": incident.incident_id,
                "execution_id": execution_id,
                "correlation_id": incident.correlation_id,
                "severity": str(incident.severity),
                "affected_services": list(incident.affected_services),
                "max_iterations": self.max_iterations,
            },
            emitter_context,
        )
        bound_log.info("investigation_started", max_iterations=self.max_iterations)

        # ── GraphRAG: enrich context with knowledge-graph evidence ────────
        # Run once before the evidence loop.  Failures are tolerated —
        # investigation continues with evidence-only context.
        context = await self._enrich_context_with_graph(
            incident, context, bound_log
        )

        evidence_results: list[EvidenceOrchestrationResult] = []
        already_requested: set[EvidenceSourceKind] = set()
        rca: RootCauseAnalysis | None = None
        cancelled = False
        timed_out = False
        iteration = 0

        # Accumulate evidence across iterations — later iterations ADD to
        # the collection rather than replacing it.
        accumulated_items: list[Evidence] = []
        accumulated_correlations: list[EvidenceCorrelation] = []

        for iteration in range(1, self.max_iterations + 1):
            # ── Cancellation check ────────────────────────────────────────
            if cancellation_check is not None and cancellation_check():
                bound_log.warning("investigation_cancelled", iteration=iteration)
                cancelled = True
                break

            # ── Timeout check ─────────────────────────────────────────────
            elapsed = (time.monotonic() - total_start) * 1000
            remaining_budget = (
                self.iteration_timeout_seconds * self.max_iterations * 1000 - elapsed
            )
            if remaining_budget <= 0:
                bound_log.warning("investigation_timed_out", iteration=iteration)
                timed_out = True
                break

            # ── Determine evidence kinds for this iteration ────────────────
            source_kinds: set[EvidenceSourceKind] | None = None
            if iteration > 1 and rca is not None:
                # Only request kinds NOT yet collected
                source_kinds = self.additional_selector.select(
                    rca,
                    evidence_results[-1].normalized if evidence_results else _empty_normalized(
                        incident.incident_id
                    ),
                    already_requested,
                )
                if not source_kinds:
                    bound_log.info(
                        "no_additional_evidence_kinds",
                        iteration=iteration,
                    )
                    break
                already_requested.update(source_kinds)

            await self._emit(
                INVESTIGATION_ITERATION_STARTED,
                {
                    "incident_id": incident.incident_id,
                    "execution_id": execution_id,
                    "iteration": iteration,
                    "source_kinds": (
                        [k.value for k in source_kinds] if source_kinds else None
                    ),
                },
                emitter_context,
            )
            bound_log.info("iteration_started", iteration=iteration)

            # ── Evidence collection ───────────────────────────────────────
            try:
                evidence_result = await asyncio.wait_for(
                    self.evidence_orchestrator.collect(
                        incident,
                        source_kinds=source_kinds,
                        context=context,
                        emitter_context=emitter_context,
                    ),
                    timeout=self.iteration_timeout_seconds,
                )
            except TimeoutError:
                bound_log.warning("iteration_evidence_timeout", iteration=iteration)
                timed_out = True
                break

            evidence_results.append(evidence_result)

            # Merge with previous iterations' evidence
            accumulated_items.extend(evidence_result.collection.items)
            accumulated_correlations.extend(evidence_result.collection.correlations)

            # Build a merged collection for the hypothesis engine
            # Deduplicate across iterations
            seen_ids: set[str] = set()
            unique_items = []
            for item in accumulated_items:
                if item.evidence_id not in seen_ids:
                    seen_ids.add(item.evidence_id)
                    unique_items.append(item)
            unique_items.sort(key=lambda e: e.relevance_score, reverse=True)

            merged_collection = EvidenceCollection(
                incident_id=incident.incident_id,
                items=tuple(unique_items),
                correlations=tuple(accumulated_correlations),
                collected_at=datetime.now(UTC),
                collection_duration_ms=sum(
                    r.duration_ms for r in evidence_results
                ),
                sources_consulted=tuple(
                    name
                    for r in evidence_results
                    for name in r.collection.sources_consulted
                ),
            )

            # ── Hypothesis generation + RCA ───────────────────────────────
            try:
                rca = await asyncio.wait_for(
                    self.hypothesis_engine.analyze(
                        incident,
                        merged_collection,
                        context=context,
                    ),
                    timeout=self.iteration_timeout_seconds,
                )
            except TimeoutError:
                bound_log.warning("iteration_rca_timeout", iteration=iteration)
                timed_out = True
                break

            await self._emit(
                HYPOTHESES_GENERATED,
                {
                    "incident_id": incident.incident_id,
                    "execution_id": execution_id,
                    "iteration": iteration,
                    "hypothesis_count": len(rca.candidate_hypotheses),
                    "confidence": rca.confidence,
                },
                emitter_context,
            )
            bound_log.info(
                "hypotheses_generated",
                iteration=iteration,
                hypothesis_count=len(rca.candidate_hypotheses),
                confidence=rca.confidence,
            )

            await self._emit(
                INVESTIGATION_ITERATION_COMPLETED,
                {
                    "incident_id": incident.incident_id,
                    "execution_id": execution_id,
                    "iteration": iteration,
                    "confidence": rca.confidence,
                    "root_cause_found": rca.has_root_cause,
                    "evidence_count": merged_collection.count,
                },
                emitter_context,
            )

            # ── Convergence check ─────────────────────────────────────────
            if rca.confidence >= self.confidence_threshold:
                bound_log.info(
                    "investigation_converged",
                    iteration=iteration,
                    confidence=rca.confidence,
                )
                break

            if iteration < self.max_iterations:
                bound_log.info(
                    "investigation_requesting_more_evidence",
                    iteration=iteration,
                    confidence=rca.confidence,
                    threshold=self.confidence_threshold,
                )

        # ── Build final result ────────────────────────────────────────────
        duration_ms = (time.monotonic() - total_start) * 1000
        inconclusive = (rca is None) or (not rca.has_root_cause)

        if rca is None:
            # Create a minimal inconclusive RCA so result always carries one
            rca = RootCauseAnalysis(
                rca_id=str(uuid.uuid4()),
                incident_id=incident.incident_id,
                observed_symptoms=incident.symptoms,
                correlated_evidence_ids=(),
                candidate_hypotheses=(),
                evaluated_hypotheses=(),
                root_cause=None,
                confidence=0.0,
                unresolved_uncertainty=(
                    "Investigation was cancelled or timed out before evidence could be gathered."
                ),
                produced_at=datetime.now(UTC),
            )

        # Emit final RCA event
        if inconclusive:
            await self._emit(
                RCA_INCONCLUSIVE,
                {
                    "incident_id": incident.incident_id,
                    "execution_id": execution_id,
                    "iterations": iteration,
                    "confidence": rca.confidence,
                    "uncertainty": rca.unresolved_uncertainty,
                },
                emitter_context,
            )
            bound_log.warning(
                "rca_inconclusive",
                iterations=iteration,
                confidence=rca.confidence,
            )
        else:
            await self._emit(
                ROOT_CAUSE_IDENTIFIED,
                {
                    "incident_id": incident.incident_id,
                    "execution_id": execution_id,
                    "iterations": iteration,
                    "confidence": rca.confidence,
                    "root_cause": rca.root_cause_title,
                },
                emitter_context,
            )
            bound_log.info(
                "root_cause_identified",
                iterations=iteration,
                confidence=rca.confidence,
                root_cause=rca.root_cause_title,
            )

        return InvestigationResult(
            rca=rca,
            evidence_results=tuple(evidence_results),
            iterations=iteration,
            inconclusive=inconclusive,
            cancelled=cancelled,
            timed_out=timed_out,
            duration_ms=duration_ms,
            incident_id=incident.incident_id,
            execution_id=execution_id,
        )

    async def _emit(
        self,
        event_name: str,
        payload: dict[str, Any],
        context: Any,
    ) -> None:
        if self.event_emitter is None:
            return
        try:
            await self.event_emitter.emit(event_name, payload, context)
        except Exception as exc:
            log.warning("event_emission_failed", event=event_name, error=str(exc))

    async def _enrich_context_with_graph(
        self,
        incident: Incident,
        context: dict[str, Any] | None,
        bound_log: Any,
    ) -> dict[str, Any] | None:
        """Run GraphRAG retrieval and inject results into the context dict.

        Does nothing when ``retrieval_pipeline`` is not set.  Failures are
        logged at WARNING level and the original context is returned unchanged
        so the investigation continues with evidence-only context.
        """
        if self.retrieval_pipeline is None:
            return context

        from backend.retrieval.context import RetrievalContext
        from backend.retrieval.models import RetrievalStrategy

        # Use the first affected service as the graph anchor.
        anchor_id = (
            incident.affected_services[0] if incident.affected_services else incident.incident_id
        )
        retrieval_ctx = RetrievalContext(
            retrieval_id=str(uuid.uuid4()),
            query=incident.title,
            strategy=RetrievalStrategy.GRAPH,
            top_k=20,
            tenant_id=incident.tenant_id,
            correlation_id=incident.correlation_id,
            filters={"anchor_id": anchor_id},
            use_cache=True,
        )

        try:
            results, metadata = await self.retrieval_pipeline.run(retrieval_ctx)
            graph_snippets = [r.chunk.content for r in results]
            latency_ms = (
                round(metadata.latency_ms, 2)
                if metadata.latency_ms is not None
                else None
            )
            bound_log.info(
                "graph_retrieval_complete",
                result_count=len(results),
                latency_ms=latency_ms,
                cached=metadata.cached,
            )
            enriched = dict(context or {})
            enriched["retrieval_context"] = {
                "graph_snippets": graph_snippets,
                "anchor_id": anchor_id,
                "result_count": len(results),
                "retrieval_id": retrieval_ctx.retrieval_id,
            }
            return enriched
        except Exception as exc:
            bound_log.warning(
                "graph_retrieval_failed",
                error=str(exc),
                anchor_id=anchor_id,
            )
            return context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_normalized(incident_id: str) -> NormalizedEvidenceCollection:
    from backend.services.evidence_normalizer import NormalizedEvidenceCollection
    return NormalizedEvidenceCollection(
        incident_id=incident_id,
        items=(),
    )
