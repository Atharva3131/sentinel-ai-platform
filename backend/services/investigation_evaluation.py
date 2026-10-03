"""Wires the EvaluationEngine into the investigation orchestration path.

``InvestigationEvaluator`` wraps the investigation result (from either the
deterministic or LLM-backed agent) and produces an EvaluationReport using the
registered investigation strategies.

Usage::

    evaluator = InvestigationEvaluator(engine=evaluation_engine)
    report = await evaluator.evaluate(
        incident=incident,
        result=investigation_result,
        agent_output=agent_rca_output,
        latency_ms=elapsed_ms,
    )

The evaluator is injected into InvestigationOrchestrator when evaluation is
desired; it remains optional so the orchestrator is not forced to depend on
the evaluation framework.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from backend.evaluation.context import EvaluationContext
from backend.evaluation.engine import EvaluationEngine
from backend.evaluation.models import EvaluationReport, EvaluationSubject
from backend.models.incident import Incident
from backend.services.investigation_agent import AgentRCAOutput
from backend.services.investigation_orchestrator import InvestigationResult

_DEFAULT_STRATEGIES = [
    "investigation_latency",
    "llm_token_usage",
    "tool_call_count",
    "evidence_utilization",
    "investigation_outcome",
]


@dataclass
class InvestigationEvaluator:
    """Evaluates investigation runs using the registered evaluation strategies.

    ``engine``             — EvaluationEngine with strategies registered
    ``strategy_names``     — which strategies to run (defaults to all 5)
    ``fail_fast``          — stop on first FAILED result
    ``timeout_seconds``    — per-strategy timeout
    """

    engine: EvaluationEngine
    strategy_names: list[str] | None = None
    fail_fast: bool = False
    timeout_seconds: float = 10.0

    async def evaluate(
        self,
        incident: Incident,
        result: InvestigationResult,
        agent_output: AgentRCAOutput | None = None,
        *,
        latency_ms: float = 0.0,
        execution_id: str | None = None,
    ) -> EvaluationReport:
        """Run evaluation strategies against a completed investigation.

        Args:
            incident:       The incident that was investigated.
            result:         The InvestigationResult from the orchestrator.
            agent_output:   Optional AgentRCAOutput with LLM-specific metrics.
            latency_ms:     Total investigation wall-clock time.
            execution_id:   Propagated into the EvaluationContext.
        """
        evidence = _build_evidence(result, agent_output, latency_ms)

        context = EvaluationContext(
            evaluation_id=str(uuid.uuid4()),
            subject=EvaluationSubject.AGENT,
            subject_id=incident.incident_id,
            evidence=evidence,
            workflow_id=None,
            execution_id=execution_id,
            correlation_id=incident.correlation_id,
        )

        names = self.strategy_names or _DEFAULT_STRATEGIES
        return await self.engine.evaluate(
            names,
            context,
            fail_fast=self.fail_fast,
            timeout_seconds=self.timeout_seconds,
        )


def _build_evidence(
    result: InvestigationResult,
    agent_output: AgentRCAOutput | None,
    latency_ms: float,
) -> dict[str, Any]:
    """Build the evidence dict consumed by evaluation strategies."""
    rca = result.rca

    # Evidence utilization
    all_ev = result.all_evidence
    kinds = list({e.source.kind.value for e in all_ev.items})

    # Token usage from agent output additional_context
    tokens_input = 0
    tokens_output = 0
    tokens_total = 0
    tool_calls = 0
    if agent_output is not None:
        ctx = agent_output.additional_context or {}
        tokens_input = int(ctx.get("tokens_input", 0))
        tokens_output = int(ctx.get("tokens_output", 0))
        tokens_total = int(ctx.get("tokens_total", tokens_input + tokens_output))
        tool_calls = len(agent_output.requested_kinds)

    return {
        "investigation_latency_ms": latency_ms,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "tokens_total": tokens_total,
        "tool_calls": tool_calls,
        "evidence_count": all_ev.count,
        "evidence_kinds": kinds,
        "hypothesis_count": len(rca.candidate_hypotheses) if rca else 0,
        "confidence": rca.confidence if rca else 0.0,
        "has_root_cause": rca.has_root_cause if rca else False,
        "iterations": result.iterations,
        "outcome": (
            "resolved" if (rca and rca.has_root_cause)
            else "failed" if (result.cancelled or result.timed_out)
            else "inconclusive"
        ),
    }
