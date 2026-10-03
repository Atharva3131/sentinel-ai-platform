"""IncidentInvestigationAgent — agent-framework integration for RCA.

This module connects the existing Agent Framework and Tool Framework to the
investigation pipeline.  The agent:

  1. Receives the current evidence collection and incident context.
  2. Identifies which evidence categories are present and missing.
  3. Can request additional evidence through the EvidenceRequestTool.
  4. Evaluates hypotheses from the HypothesisEngine.
  5. Produces a structured AgentRCAOutput.

Design constraints:
  * The agent NEVER accesses provider SDKs directly — it uses the Tool Framework
    (EvidenceRequestTool) which delegates to the EvidenceOrchestrator.
  * The agent NEVER bypasses the Runtime.
  * The agent does not contain hard-coded failure-mode logic.
  * When no LLM agent runner is available, the DeterministicInvestigationAgent
    fallback produces a deterministic output suitable for tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import structlog

from backend.core.hypothesis_engine import HypothesisEngine
from backend.models.evidence import EvidenceCollection, EvidenceSourceKind
from backend.models.hypothesis import RootCauseAnalysis
from backend.models.incident import Incident
from backend.services.evidence_normalizer import NormalizedEvidenceCollection
from backend.services.evidence_orchestrator import EvidenceOrchestrator

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Tool: EvidenceRequestTool
# ---------------------------------------------------------------------------


@dataclass
class EvidenceRequestTool:
    """Tool that the investigation agent uses to request additional evidence.

    The agent invokes this tool instead of calling provider SDKs directly.
    This preserves the Tool Framework indirection and keeps the agent
    decoupled from concrete provider implementations.
    """

    orchestrator: EvidenceOrchestrator
    name: str = "request_evidence"
    description: str = (
        "Request additional evidence from registered providers. "
        "Specify which evidence categories (source_kinds) are needed. "
        "Returns the count of new evidence items collected."
    )

    async def execute(
        self,
        incident: Incident,
        source_kinds: set[EvidenceSourceKind],
        *,
        context: dict[str, Any] | None = None,
        emitter_context: Any = None,
    ) -> dict[str, Any]:
        """Collect evidence for the requested kinds and return a summary."""
        result = await self.orchestrator.collect(
            incident,
            source_kinds=source_kinds,
            context=context,
            emitter_context=emitter_context,
        )
        return {
            "evidence_count": result.collection.count,
            "provider_errors": result.provider_errors,
            "partial": result.partial,
            "kinds_collected": [k.value for k in source_kinds],
        }


# ---------------------------------------------------------------------------
# Agent output model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentRCAOutput:
    """Structured output produced by the investigation agent.

    ``rca``                — the full RootCauseAnalysis from HypothesisEngine
    ``missing_evidence``   — kinds the agent identified as absent
    ``requested_kinds``    — kinds the agent requested during its run
    ``reasoning_summary``  — human-readable summary of the agent's reasoning
    ``additional_context`` — free-form dict for LLM-produced supplementary data
    """

    rca: RootCauseAnalysis
    missing_evidence: tuple[EvidenceSourceKind, ...]
    requested_kinds: tuple[EvidenceSourceKind, ...]
    reasoning_summary: str
    additional_context: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# InvestigationAgent protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class InvestigationAgent(Protocol):
    """Port satisfied by both the deterministic fallback and LLM-backed agents."""

    async def investigate(
        self,
        incident: Incident,
        evidence: EvidenceCollection,
        normalized: NormalizedEvidenceCollection,
        *,
        context: dict[str, Any] | None = None,
    ) -> AgentRCAOutput: ...


# ---------------------------------------------------------------------------
# Deterministic fallback — no LLM required
# ---------------------------------------------------------------------------


class DeterministicInvestigationAgent:
    """Deterministic investigation agent for tests and LLM-unavailable scenarios.

    Uses HypothesisEngine with DeterministicHypothesisGenerator to evaluate
    the current evidence and optionally request missing categories.

    This agent does NOT hard-code failure-mode rules.  Its "missing evidence"
    detection simply identifies which EvidenceSourceKind categories have zero
    items in the current collection.
    """

    def __init__(
        self,
        hypothesis_engine: HypothesisEngine,
        evidence_tool: EvidenceRequestTool | None = None,
        *,
        request_missing: bool = True,
        max_tool_calls: int = 2,
    ) -> None:
        self._engine = hypothesis_engine
        self._evidence_tool = evidence_tool
        self._request_missing = request_missing
        self._max_tool_calls = max_tool_calls

    async def investigate(
        self,
        incident: Incident,
        evidence: EvidenceCollection,
        normalized: NormalizedEvidenceCollection,
        *,
        context: dict[str, Any] | None = None,
    ) -> AgentRCAOutput:
        """Run deterministic investigation and return structured RCA output."""
        bound_log = log.bind(
            incident_id=incident.incident_id,
            agent="DeterministicInvestigationAgent",
        )

        current_evidence = evidence
        requested_kinds: list[EvidenceSourceKind] = []
        tool_calls = 0

        # Identify missing evidence categories
        present_kinds = {item.source.kind for item in current_evidence.items}
        important_kinds = {
            EvidenceSourceKind.METRICS,
            EvidenceSourceKind.LOGS,
            EvidenceSourceKind.DEPLOYMENTS,
        }
        missing = important_kinds - present_kinds

        # Optionally request missing evidence via the tool
        if missing and self._request_missing and self._evidence_tool is not None:
            while missing and tool_calls < self._max_tool_calls:
                to_request = missing.copy()
                bound_log.info(
                    "agent_requesting_evidence",
                    kinds=[k.value for k in to_request],
                )
                tool_result = await self._evidence_tool.execute(
                    incident,
                    to_request,
                    context=context,
                )
                requested_kinds.extend(to_request)
                tool_calls += 1

                # Re-collect to get updated evidence from the orchestrator
                new_result = await self._evidence_tool.orchestrator.collect(
                    incident,
                    source_kinds=to_request,
                    context=context,
                )
                # Merge new items with current evidence
                existing_ids = {e.evidence_id for e in current_evidence.items}
                new_items = [
                    e for e in new_result.collection.items
                    if e.evidence_id not in existing_ids
                ]
                all_items = list(current_evidence.items) + new_items
                all_items.sort(key=lambda e: e.relevance_score, reverse=True)
                from backend.models.evidence import EvidenceCollection
                current_evidence = EvidenceCollection(
                    incident_id=incident.incident_id,
                    items=tuple(all_items),
                    correlations=current_evidence.correlations + new_result.collection.correlations,
                    collected_at=datetime.now(UTC),
                    collection_duration_ms=(
                        (current_evidence.collection_duration_ms or 0.0)
                        + new_result.duration_ms
                    ),
                    sources_consulted=current_evidence.sources_consulted
                    + new_result.collection.sources_consulted,
                )
                present_kinds = {item.source.kind for item in current_evidence.items}
                missing = important_kinds - present_kinds
                bound_log.debug(
                    "agent_tool_result",
                    new_items=tool_result["evidence_count"],
                    tool_calls=tool_calls,
                )

        # Run the hypothesis engine on the (possibly enriched) evidence
        rca = await self._engine.analyze(incident, current_evidence, context=context)

        # Build reasoning summary
        parts: list[str] = []
        if rca.has_root_cause and rca.root_cause is not None:
            parts.append(
                f"Root cause identified: {rca.root_cause.title} "
                f"(confidence {rca.confidence:.0%})."
            )
        else:
            parts.append(
                f"Investigation inconclusive. "
                f"Best confidence: {rca.confidence:.0%}. "
                f"{rca.unresolved_uncertainty or ''}"
            )
        if missing:
            parts.append(
                f"Missing evidence categories: {', '.join(k.value for k in missing)}."
            )
        if requested_kinds:
            parts.append(
                f"Requested additional evidence: "
                f"{', '.join(k.value for k in requested_kinds)}."
            )

        return AgentRCAOutput(
            rca=rca,
            missing_evidence=tuple(missing),
            requested_kinds=tuple(requested_kinds),
            reasoning_summary=" ".join(parts),
        )
