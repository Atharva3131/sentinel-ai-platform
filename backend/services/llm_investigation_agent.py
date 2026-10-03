"""LLM-backed IncidentInvestigationAgent.

This module provides:
  - ``LLMInvestigationAgent``       — production implementation using an LLMProvider
  - ``EvidenceGroundingValidator``  — verifies every evidence reference in an RCA
    actually exists in the available evidence collection

Architecture:
  1. The agent receives the current EvidenceCollection and IncidentContext.
  2. It builds a structured prompt from the normalised evidence.
  3. It sends the prompt to the LLMProvider with a JSON-structured output schema.
  4. The LLM may invoke the ``request_evidence`` tool to fetch additional evidence.
  5. The agent executes tool calls via the existing EvidenceRequestTool — never
     directly via provider SDKs.
  6. After at most ``max_tool_rounds`` tool call rounds, the agent forces a final
     structured RCA output.
  7. The RCA output is validated: every evidence ID referenced must appear in the
     actual EvidenceCollection — hallucinated IDs are stripped.
  8. The result is converted to the existing domain ``RootCauseAnalysis`` object.

The ``DeterministicInvestigationAgent`` from investigation_agent.py remains
available for deterministic unit tests.  The LLM agent is plug-compatible:
both satisfy the ``InvestigationAgent`` protocol.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from backend.core.hypothesis_engine import HypothesisEngine
from backend.interfaces.llm import (
    LLMCancelledError,
    LLMError,
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMToolCall,
    LLMToolDefinition,
    LLMToolResult,
)
from backend.models.evidence import EvidenceCollection, EvidenceSourceKind
from backend.models.hypothesis import (
    Hypothesis,
    HypothesisEvaluation,
    HypothesisStatus,
    RootCauseAnalysis,
)
from backend.models.incident import Incident
from backend.services.evidence_normalizer import EvidenceNormalizer, NormalizedEvidenceCollection
from backend.services.investigation_agent import AgentRCAOutput, EvidenceRequestTool

log = structlog.get_logger(__name__)

# JSON Schema for the structured RCA output the LLM must produce
_RCA_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["root_cause_title", "root_cause_description", "confidence",
                 "supporting_evidence_ids", "reasoning_summary", "missing_evidence"],
    "properties": {
        "root_cause_title": {
            "type": "string",
            "description": "Short title for the root cause hypothesis (max 200 chars).",
        },
        "root_cause_description": {
            "type": "string",
            "description": "Detailed description of why this is the root cause.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence score 0.0-1.0 for the root cause hypothesis.",
        },
        "supporting_evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "List of evidence_id values from the provided evidence that support "
                "this root cause.  Only use IDs that were actually provided to you."
            ),
        },
        "contradicting_evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Evidence IDs that contradict this hypothesis.",
        },
        "missing_evidence": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [k.value for k in EvidenceSourceKind],
            },
            "description": "Evidence categories that would improve confidence if available.",
        },
        "reasoning_summary": {
            "type": "string",
            "description": "Step-by-step explanation of how you reached this conclusion.",
        },
    },
}

# Tool definition exposed to the LLM
_REQUEST_EVIDENCE_TOOL = LLMToolDefinition(
    name="request_evidence",
    description=(
        "Request additional evidence categories from the monitoring systems. "
        "Call this when you need more data to confirm or refute a hypothesis. "
        "Specify which evidence source kinds you need."
    ),
    input_schema={
        "type": "object",
        "required": ["source_kinds"],
        "properties": {
            "source_kinds": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [k.value for k in EvidenceSourceKind],
                },
                "description": "Evidence categories to collect.",
            }
        },
    },
)


# ---------------------------------------------------------------------------
# Evidence Grounding Validator
# ---------------------------------------------------------------------------


class EvidenceGroundingValidator:
    """Validates that every evidence ID referenced by the LLM exists in reality.

    The LLM must NOT invent evidence IDs.  Any ID that does not appear in the
    actual EvidenceCollection is stripped from the hypothesis and a warning is
    logged.  This preserves the safety invariant that every evidence reference
    in an RCA points to something real.
    """

    def validate(
        self,
        evidence_ids: list[str],
        collection: EvidenceCollection,
        *,
        incident_id: str,
        field_name: str = "supporting_evidence_ids",
    ) -> tuple[str, ...]:
        """Return only the IDs that actually exist in *collection*.

        Strips hallucinated IDs and logs a warning for each one removed.
        """
        known = {e.evidence_id for e in collection.items}
        valid: list[str] = []
        for eid in evidence_ids:
            if eid in known:
                valid.append(eid)
            else:
                log.warning(
                    "llm_hallucinated_evidence_id",
                    evidence_id=eid,
                    field=field_name,
                    incident_id=incident_id,
                )
        return tuple(valid)


# ---------------------------------------------------------------------------
# LLMInvestigationAgent
# ---------------------------------------------------------------------------


@dataclass
class LLMInvestigationAgent:
    """Production investigation agent backed by an LLMProvider.

    Satisfies the ``InvestigationAgent`` protocol (structural).

    Inject:
      ``llm_provider``         — LLMProvider implementation
      ``evidence_tool``        — EvidenceRequestTool for safe provider access
      ``hypothesis_engine``    — HypothesisEngine for fallback/validation
      ``model``                — model name to request (provider default when None)
      ``max_tool_rounds``      — max tool call iterations before forcing final answer
      ``acceptance_threshold`` — minimum confidence to consider RCA conclusive
      ``system_prompt``        — optional override; a sensible default is provided
    """

    llm_provider: LLMProvider
    evidence_tool: EvidenceRequestTool | None = None
    hypothesis_engine: HypothesisEngine | None = None
    model: str | None = None
    max_tool_rounds: int = 3
    acceptance_threshold: float = 0.60
    system_prompt: str | None = None
    _normalizer: EvidenceNormalizer = field(default_factory=EvidenceNormalizer)
    _grounder: EvidenceGroundingValidator = field(
        default_factory=EvidenceGroundingValidator
    )

    async def investigate(
        self,
        incident: Incident,
        evidence: EvidenceCollection,
        normalized: NormalizedEvidenceCollection,
        *,
        context: dict[str, Any] | None = None,
    ) -> AgentRCAOutput:
        """Run LLM-backed investigation and return grounded RCA output.

        The agent:
          1. Builds a system prompt describing its task and the incident.
          2. Builds a user message with all normalised evidence items.
          3. Sends to the LLM with the ``request_evidence`` tool available.
          4. If the LLM calls ``request_evidence``, executes via EvidenceRequestTool,
             extends the evidence, and re-prompts.
          5. After ``max_tool_rounds`` or when the LLM stops calling tools,
             forces a structured JSON final answer.
          6. Grounds all evidence IDs — strips any hallucinated references.
          7. Returns an AgentRCAOutput with a domain RootCauseAnalysis.
        """
        bound_log = log.bind(
            incident_id=incident.incident_id,
            agent="LLMInvestigationAgent",
            model=self.model,
        )

        messages: list[LLMMessage] = [
            LLMMessage(role="system", content=self._build_system_prompt(incident)),
            LLMMessage(role="user", content=self._build_evidence_prompt(incident, normalized)),
        ]

        current_evidence = evidence
        current_normalized = normalized
        requested_kinds: list[EvidenceSourceKind] = []
        tool_rounds = 0
        cancellation_token = (context or {}).get("cancellation_token")

        while tool_rounds < self.max_tool_rounds:
            tools = (_REQUEST_EVIDENCE_TOOL,) if self.evidence_tool is not None else ()
            request = LLMRequest(
                messages=tuple(messages),
                model=self.model,
                tools=tools,
                tool_choice="auto" if tools else None,
                timeout_seconds=(context or {}).get("timeout_seconds"),
                cancellation_token=cancellation_token,
                context=_make_provider_context(incident, context),
            )

            try:
                response = await self.llm_provider.generate(request)
            except LLMCancelledError:
                bound_log.warning("llm_investigation_cancelled")
                return self._fallback_output(
                    incident, current_evidence, "Investigation cancelled."
                )
            except LLMError as exc:
                bound_log.warning("llm_investigation_error", error=str(exc))
                return self._fallback_output(
                    incident, current_evidence, f"LLM unavailable: {exc}"
                )

            # No tool calls → proceed to structured final answer
            if not response.has_tool_calls:
                bound_log.debug("llm_no_tool_calls", tool_rounds=tool_rounds)
                break

            # Execute tool calls
            tool_round_results: list[LLMToolResult] = []
            for tc in response.tool_calls:
                tool_result = await self._execute_tool_call(
                    tc, incident, context=context
                )
                tool_round_results.append(tool_result)
                if tc.tool_name == "request_evidence":
                    kinds_requested = tc.arguments.get("source_kinds", [])
                    for k in kinds_requested:
                        try:
                            requested_kinds.append(EvidenceSourceKind(k))
                        except ValueError:
                            pass

            # Extend the evidence collection with newly gathered items
            if self.evidence_tool is not None and requested_kinds:
                new_result = await self.evidence_tool.orchestrator.collect(
                    incident,
                    source_kinds=set(requested_kinds[-len(response.tool_calls):]),
                    context=context,
                )
                # Merge deduplicating by evidence_id
                existing_ids = {e.evidence_id for e in current_evidence.items}
                new_items = [
                    e for e in new_result.collection.items
                    if e.evidence_id not in existing_ids
                ]
                all_items = list(current_evidence.items) + new_items
                all_items.sort(key=lambda e: e.relevance_score, reverse=True)
                current_evidence = EvidenceCollection(
                    incident_id=incident.incident_id,
                    items=tuple(all_items),
                    correlations=current_evidence.correlations
                    + new_result.collection.correlations,
                    collected_at=datetime.now(UTC),
                    collection_duration_ms=(
                        (current_evidence.collection_duration_ms or 0.0)
                        + new_result.duration_ms
                    ),
                    sources_consulted=current_evidence.sources_consulted
                    + new_result.collection.sources_consulted,
                )
                current_normalized = self._normalizer.normalize_collection(current_evidence)

            # Append assistant + tool result messages for the next round
            if response.tool_calls:
                messages.append(
                    LLMMessage(role="assistant", content=response.content or "")
                )
                for tr in tool_round_results:
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=tr.content,
                            tool_call_id=tr.call_id,
                        )
                    )
                # Update evidence prompt with new items
                messages.append(
                    LLMMessage(
                        role="user",
                        content=self._build_evidence_prompt(
                            incident, current_normalized, update=True
                        ),
                    )
                )

            tool_rounds += 1

        # Force structured final answer
        structured = await self._get_structured_answer(
            incident=incident,
            messages=messages,
            evidence=current_evidence,
            normalized=current_normalized,
            cancellation_token=cancellation_token,
            context=context,
        )

        bound_log.info(
            "llm_investigation_complete",
            confidence=structured.get("confidence", 0.0),
            tool_rounds=tool_rounds,
            evidence_count=current_evidence.count,
        )

        return self._build_output(
            incident=incident,
            evidence=current_evidence,
            structured=structured,
            requested_kinds=requested_kinds,
        )

    async def _get_structured_answer(
        self,
        incident: Incident,
        messages: list[LLMMessage],
        evidence: EvidenceCollection,
        normalized: NormalizedEvidenceCollection,
        cancellation_token: Any,
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Send a final structured-output request and return the parsed dict."""
        final_messages = [
            *messages,
            LLMMessage(
                role="user",
                content=(
                    "Based on all available evidence, produce your final root cause analysis. "
                    "Reference only evidence IDs that were provided to you. "
                    "Respond using the required JSON schema."
                ),
            ),
        ]
        request = LLMRequest(
            messages=tuple(final_messages),
            model=self.model,
            tools=(),
            structured_schema=_RCA_OUTPUT_SCHEMA,
            timeout_seconds=(context or {}).get("timeout_seconds"),
            cancellation_token=cancellation_token,
            context=_make_provider_context(incident, context),
        )
        try:
            response = await self.llm_provider.generate(request)
            if response.structured_output:
                return response.structured_output
            # Try parsing the content as JSON
            parsed: dict[str, Any] = json.loads(response.content)
            return parsed
        except (LLMError, json.JSONDecodeError, ValueError) as exc:
            log.warning(
                "llm_structured_answer_failed",
                incident_id=incident.incident_id,
                error=str(exc),
            )
            return {
                "root_cause_title": "Investigation inconclusive",
                "root_cause_description": f"LLM could not produce a structured answer: {exc}",
                "confidence": 0.0,
                "supporting_evidence_ids": [],
                "contradicting_evidence_ids": [],
                "missing_evidence": [],
                "reasoning_summary": str(exc),
            }

    async def _execute_tool_call(
        self,
        tc: LLMToolCall,
        incident: Incident,
        *,
        context: dict[str, Any] | None,
    ) -> LLMToolResult:
        """Execute a tool call and return a result for the model."""
        if tc.tool_name == "request_evidence" and self.evidence_tool is not None:
            kinds_raw = tc.arguments.get("source_kinds", [])
            kinds: set[EvidenceSourceKind] = set()
            for k in kinds_raw:
                try:
                    kinds.add(EvidenceSourceKind(k))
                except ValueError:
                    pass
            if kinds:
                result = await self.evidence_tool.execute(
                    incident, kinds, context=context
                )
                content = (
                    f"Collected {result['evidence_count']} evidence items "
                    f"from {', '.join(k.value for k in kinds)}."
                    + (
                        f" Errors: {result['provider_errors']}"
                        if result.get("provider_errors")
                        else ""
                    )
                )
                return LLMToolResult(
                    call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    content=content,
                )
        return LLMToolResult(
            call_id=tc.call_id,
            tool_name=tc.tool_name,
            content=f"Tool '{tc.tool_name}' is not available.",
            is_error=True,
        )

    def _build_output(
        self,
        incident: Incident,
        evidence: EvidenceCollection,
        structured: dict[str, Any],
        requested_kinds: list[EvidenceSourceKind],
    ) -> AgentRCAOutput:
        """Convert the structured LLM output to domain objects, grounding evidence IDs."""
        now = datetime.now(UTC)
        confidence = float(structured.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))

        raw_support = structured.get("supporting_evidence_ids", [])
        raw_contra = structured.get("contradicting_evidence_ids", [])
        supporting = self._grounder.validate(
            raw_support, evidence,
            incident_id=incident.incident_id,
            field_name="supporting_evidence_ids",
        )
        contradicting = self._grounder.validate(
            raw_contra, evidence,
            incident_id=incident.incident_id,
            field_name="contradicting_evidence_ids",
        )

        missing_raw = structured.get("missing_evidence", [])
        missing: tuple[EvidenceSourceKind, ...] = tuple(
            EvidenceSourceKind(k) for k in missing_raw
            if k in {v.value for v in EvidenceSourceKind}
        )

        # Build a Hypothesis from the LLM's output
        hypothesis = Hypothesis(
            hypothesis_id=str(uuid.uuid4()),
            incident_id=incident.incident_id,
            title=str(structured.get("root_cause_title", "Unknown")),
            description=str(structured.get("root_cause_description", "")),
            proposed_at=now,
            supporting_evidence_ids=supporting,
            contradicting_evidence_ids=contradicting,
        )

        # Evaluate via the domain rules (grounded confidence)
        if confidence >= self.acceptance_threshold:
            status = HypothesisStatus.SUPPORTED
        elif confidence < 0.20:
            status = HypothesisStatus.REFUTED
        else:
            status = HypothesisStatus.INCONCLUSIVE

        evaluation = HypothesisEvaluation(
            hypothesis_id=hypothesis.hypothesis_id,
            status=status,
            confidence=round(confidence, 4),
            supporting_evidence_ids=supporting,
            contradicting_evidence_ids=contradicting,
            reasoning=str(structured.get("reasoning_summary", "")),
            evaluated_at=now,
            evaluator="llm-investigation-agent",
        )
        evaluated_hypothesis = hypothesis.with_evaluation(evaluation)

        root_cause = evaluated_hypothesis if confidence >= self.acceptance_threshold else None
        uncertainty = (
            None
            if root_cause is not None
            else (
                f"LLM confidence {confidence:.0%} did not reach threshold "
                f"{self.acceptance_threshold:.0%}. "
                f"{structured.get('reasoning_summary', '')}"
            )
        )

        rca = RootCauseAnalysis(
            rca_id=str(uuid.uuid4()),
            incident_id=incident.incident_id,
            observed_symptoms=incident.symptoms,
            correlated_evidence_ids=tuple(e.evidence_id for e in evidence.items),
            candidate_hypotheses=(hypothesis,),
            evaluated_hypotheses=(evaluated_hypothesis,),
            root_cause=root_cause,
            confidence=confidence,
            unresolved_uncertainty=uncertainty,
            produced_at=now,
        )

        return AgentRCAOutput(
            rca=rca,
            missing_evidence=missing,
            requested_kinds=tuple(requested_kinds),
            reasoning_summary=str(structured.get("reasoning_summary", "")),
            additional_context={
                "model": self.model,
                "tool_rounds": len(requested_kinds),
                "evidence_count": evidence.count,
            },
        )

    def _fallback_output(
        self,
        incident: Incident,
        evidence: EvidenceCollection,
        reason: str,
    ) -> AgentRCAOutput:
        """Return an inconclusive AgentRCAOutput when the LLM is unavailable."""
        now = datetime.now(UTC)
        rca = RootCauseAnalysis(
            rca_id=str(uuid.uuid4()),
            incident_id=incident.incident_id,
            observed_symptoms=incident.symptoms,
            correlated_evidence_ids=tuple(e.evidence_id for e in evidence.items),
            candidate_hypotheses=(),
            evaluated_hypotheses=(),
            root_cause=None,
            confidence=0.0,
            unresolved_uncertainty=reason,
            produced_at=now,
        )
        return AgentRCAOutput(
            rca=rca,
            missing_evidence=(),
            requested_kinds=(),
            reasoning_summary=reason,
        )

    def _build_system_prompt(self, incident: Incident) -> str:
        if self.system_prompt:
            return self.system_prompt
        return (
            "You are an expert SRE investigating a production incident. "
            "Your goal is to identify the root cause based on the evidence provided. "
            "You may call the 'request_evidence' tool to fetch additional monitoring data "
            "before reaching your conclusion. "
            "When you have enough information, produce a structured JSON root cause analysis. "
            "IMPORTANT: Only reference evidence IDs that were explicitly provided to you — "
            "never invent or guess evidence IDs."
        )

    def _build_evidence_prompt(
        self,
        incident: Incident,
        normalized: NormalizedEvidenceCollection,
        *,
        update: bool = False,
    ) -> str:
        prefix = "Here is the updated evidence after your tool call:" if update else (
            f"Incident: {incident.title}\n"
            f"Severity: {incident.severity}\n"
            f"Affected services: {', '.join(incident.affected_services)}\n"
            f"Symptoms: "
            f"{'; '.join(incident.symptoms) if incident.symptoms else 'none reported'}"
            "\n\n"
            "Evidence collected:"
        )
        if not normalized.items:
            return prefix + "\n(No evidence available)"

        lines = [prefix]
        for item in normalized.items[:30]:  # cap at 30 items to fit context
            lines.append(
                f"\n[{item.evidence_id}] "
                f"type={item.evidence_type} "
                f"service={item.service or 'unknown'} "
                f"confidence={item.confidence:.2f}\n"
                f"  {item.title}: {item.content[:300]}"
            )
        if len(normalized.items) > 30:
            lines.append(f"\n... ({len(normalized.items) - 30} more evidence items)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider_context(
    incident: Incident, context: dict[str, Any] | None
) -> Any:
    from backend.interfaces.common import ProviderContext
    ctx = context or {}
    return ProviderContext(
        correlation_id=incident.correlation_id,
        workflow_id=ctx.get("workflow_id"),
        execution_id=ctx.get("execution_id"),
    )
