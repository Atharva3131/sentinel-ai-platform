"""Investigation evaluation strategies.

These strategies integrate the LLM investigation path with the
existing Evaluation Framework, recording:
  - investigation latency
  - LLM model usage and token counts
  - tool call count
  - evidence utilization ratio
  - hypothesis count
  - final confidence
  - investigation outcome (root cause found / inconclusive)

Each strategy is a stateless EvaluationStrategy implementation that reads
evidence from EvaluationContext.evidence using well-known keys.

Evidence key contract (set by InvestigationEvaluationContext helper):
  investigation_latency_ms    float
  model_name                  str | None
  tokens_input                int
  tokens_output               int
  tokens_total                int
  tool_calls                  int
  evidence_count              int
  evidence_kinds              list[str]
  hypothesis_count            int
  confidence                  float
  has_root_cause              bool
  iterations                  int
  outcome                     "resolved" | "inconclusive" | "failed"
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.evaluation.context import EvaluationContext
from backend.evaluation.models import (
    EvaluationMetric,
    EvaluationMetricKind,
    EvaluationResult,
    EvaluationStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _metric(
    name: str,
    kind: EvaluationMetricKind,
    value: float,
    score: float,
    *,
    passed: bool,
    unit: str | None = None,
    threshold: float | None = None,
    weight: float = 1.0,
) -> EvaluationMetric:
    return EvaluationMetric(
        name=name,
        kind=kind,
        value=value,
        score=_clamp(score),
        passed=passed,
        unit=unit,
        threshold=threshold,
        weight=weight,
    )


def _result(
    strategy_name: str,
    strategy_version: str | None,
    status: EvaluationStatus,
    metrics: tuple[EvaluationMetric, ...],
    *,
    started_at: datetime,
    reasoning: str | None = None,
) -> EvaluationResult:
    score = EvaluationResult.aggregate_score(metrics) if metrics else 0.0
    confidence = min(m.score for m in metrics) if metrics else 1.0
    refs = tuple(m.name for m in metrics)
    return EvaluationResult(
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        status=status,
        metrics=metrics,
        score=_clamp(score),
        confidence=_clamp(confidence),
        evidence_refs=refs,
        started_at=started_at,
        ended_at=_now(),
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# InvestigationLatencyStrategy
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InvestigationLatencyStrategy:
    """Scores investigation latency against a configurable threshold."""

    threshold_ms: float = 30_000.0   # 30 s default — investigations may be slow
    weight: float = 1.0

    name = "investigation_latency"
    version = "1.0.0"

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        started = _now()
        latency = float(context.evidence.get("investigation_latency_ms", 0.0))
        passed = latency <= self.threshold_ms
        score = _clamp(1.0 - (latency / (self.threshold_ms * 2.0))) if latency > 0 else 1.0
        metrics = (
            _metric(
                "investigation_latency_ms",
                EvaluationMetricKind.LATENCY,
                latency,
                score,
                passed=passed,
                unit="ms",
                threshold=self.threshold_ms,
            ),
        )
        status = EvaluationStatus.PASSED if passed else EvaluationStatus.FAILED
        return _result(
            self.name,
            self.version,
            status,
            metrics,
            started_at=started,
            reasoning=f"Latency {latency:.0f}ms vs threshold {self.threshold_ms:.0f}ms.",
        )


# ---------------------------------------------------------------------------
# LLMTokenUsageStrategy
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LLMTokenUsageStrategy:
    """Records LLM token usage; score is inversely proportional to consumption."""

    max_total_tokens: int = 100_000
    weight: float = 1.0

    name = "llm_token_usage"
    version = "1.0.0"

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        started = _now()
        tokens_in = int(context.evidence.get("tokens_input", 0))
        tokens_out = int(context.evidence.get("tokens_output", 0))
        tokens_total = int(context.evidence.get("tokens_total", tokens_in + tokens_out))
        passed = tokens_total <= self.max_total_tokens
        score = _clamp(1.0 - tokens_total / (self.max_total_tokens * 2.0))
        metrics = (
            _metric(
                "tokens_input",
                EvaluationMetricKind.TOKEN_USAGE,
                float(tokens_in),
                _clamp(1.0 - tokens_in / max(self.max_total_tokens, 1)),
                passed=True,
                unit="tokens",
            ),
            _metric(
                "tokens_output",
                EvaluationMetricKind.TOKEN_USAGE,
                float(tokens_out),
                _clamp(1.0 - tokens_out / max(self.max_total_tokens, 1)),
                passed=True,
                unit="tokens",
            ),
            _metric(
                "tokens_total",
                EvaluationMetricKind.TOKEN_USAGE,
                float(tokens_total),
                score,
                passed=passed,
                unit="tokens",
                threshold=float(self.max_total_tokens),
            ),
        )
        status = EvaluationStatus.PASSED if passed else EvaluationStatus.FAILED
        return _result(
            self.name,
            self.version,
            status,
            metrics,
            started_at=started,
            reasoning=(
                f"Total tokens: {tokens_total} / {self.max_total_tokens}. "
                f"Input: {tokens_in}, Output: {tokens_out}."
            ),
        )


# ---------------------------------------------------------------------------
# ToolCallStrategy
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolCallStrategy:
    """Scores the tool call count (lower is more efficient)."""

    max_tool_calls: int = 6
    weight: float = 1.0

    name = "tool_call_count"
    version = "1.0.0"

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        started = _now()
        calls = int(context.evidence.get("tool_calls", 0))
        passed = calls <= self.max_tool_calls
        score = _clamp(1.0 - calls / (self.max_tool_calls * 2.0)) if calls > 0 else 1.0
        metrics = (
            _metric(
                "tool_calls",
                EvaluationMetricKind.TOOL_SUCCESS,
                float(calls),
                score,
                passed=passed,
                threshold=float(self.max_tool_calls),
            ),
        )
        status = EvaluationStatus.PASSED if passed else EvaluationStatus.FAILED
        return _result(
            self.name,
            self.version,
            status,
            metrics,
            started_at=started,
            reasoning=f"Tool calls: {calls} / {self.max_tool_calls} allowed.",
        )


# ---------------------------------------------------------------------------
# EvidenceUtilizationStrategy
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EvidenceUtilizationStrategy:
    """Scores how many evidence categories were utilised.

    A higher utilization (more evidence kinds consulted) is scored higher.
    """

    weight: float = 1.0

    name = "evidence_utilization"
    version = "1.0.0"

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        started = _now()
        evidence_count = int(context.evidence.get("evidence_count", 0))
        kinds: list[str] = list(context.evidence.get("evidence_kinds", []))
        kind_count = len(set(kinds))
        total_kinds = 9  # total EvidenceSourceKind values excluding SYNTHETIC/OTHER
        score = _clamp(kind_count / total_kinds) if total_kinds > 0 else 0.0
        passed = evidence_count > 0
        metrics = (
            _metric(
                "evidence_count",
                EvaluationMetricKind.CUSTOM,
                float(evidence_count),
                _clamp(evidence_count / 10.0),  # 10 items → full score
                passed=passed,
            ),
            _metric(
                "evidence_kind_count",
                EvaluationMetricKind.CUSTOM,
                float(kind_count),
                score,
                passed=True,
            ),
        )
        status = EvaluationStatus.PASSED if passed else EvaluationStatus.FAILED
        return _result(
            self.name,
            self.version,
            status,
            metrics,
            started_at=started,
            reasoning=(
                f"Collected {evidence_count} items across {kind_count} evidence kinds."
            ),
        )


# ---------------------------------------------------------------------------
# InvestigationOutcomeStrategy
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InvestigationOutcomeStrategy:
    """Scores the investigation outcome and hypothesis confidence."""

    confidence_threshold: float = 0.60
    weight: float = 2.0  # outcome is double-weighted

    name = "investigation_outcome"
    version = "1.0.0"

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        started = _now()
        has_root_cause = bool(context.evidence.get("has_root_cause", False))
        confidence = float(context.evidence.get("confidence", 0.0))
        hypothesis_count = int(context.evidence.get("hypothesis_count", 0))
        iterations = int(context.evidence.get("iterations", 1))

        outcome_score = confidence if has_root_cause else confidence * 0.5
        passed = has_root_cause and confidence >= self.confidence_threshold

        metrics = (
            _metric(
                "has_root_cause",
                EvaluationMetricKind.AGENT_SUCCESS,
                1.0 if has_root_cause else 0.0,
                1.0 if has_root_cause else 0.0,
                passed=has_root_cause,
            ),
            _metric(
                "rca_confidence",
                EvaluationMetricKind.AGENT_SUCCESS,
                confidence,
                outcome_score,
                passed=confidence >= self.confidence_threshold,
                threshold=self.confidence_threshold,
            ),
            _metric(
                "hypothesis_count",
                EvaluationMetricKind.CUSTOM,
                float(hypothesis_count),
                _clamp(hypothesis_count / 5.0),
                passed=hypothesis_count > 0,
            ),
            _metric(
                "investigation_iterations",
                EvaluationMetricKind.CUSTOM,
                float(iterations),
                _clamp(1.0 - (iterations - 1) / 5.0),  # fewer iterations is better
                passed=True,
            ),
        )
        status = EvaluationStatus.PASSED if passed else (
            EvaluationStatus.FAILED if not has_root_cause
            else EvaluationStatus.INDETERMINATE
        )
        return _result(
            self.name,
            self.version,
            status,
            metrics,
            started_at=started,
            reasoning=(
                f"Root cause {'found' if has_root_cause else 'not found'}. "
                f"Confidence: {confidence:.0%}. "
                f"Hypotheses: {hypothesis_count}. "
                f"Iterations: {iterations}."
            ),
        )


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


def register_investigation_strategies(registry: Any) -> None:
    """Register all investigation evaluation strategies with *registry*."""
    from backend.evaluation.registry import EvaluationRegistry

    assert isinstance(registry, EvaluationRegistry)

    def _latency(deps: Any) -> InvestigationLatencyStrategy:
        threshold = (deps or {}).get("latency_threshold_ms", 30_000.0)
        return InvestigationLatencyStrategy(threshold_ms=threshold)

    def _tokens(deps: Any) -> LLMTokenUsageStrategy:
        max_tokens = (deps or {}).get("max_total_tokens", 100_000)
        return LLMTokenUsageStrategy(max_total_tokens=max_tokens)

    def _tool_calls(deps: Any) -> ToolCallStrategy:
        max_calls = (deps or {}).get("max_tool_calls", 6)
        return ToolCallStrategy(max_tool_calls=max_calls)

    def _evidence(deps: Any) -> EvidenceUtilizationStrategy:
        return EvidenceUtilizationStrategy()

    def _outcome(deps: Any) -> InvestigationOutcomeStrategy:
        threshold = (deps or {}).get("confidence_threshold", 0.60)
        return InvestigationOutcomeStrategy(confidence_threshold=threshold)

    registry.register(
        name="investigation_latency", version="1.0.0", provider=_latency
    )
    registry.register(
        name="llm_token_usage", version="1.0.0", provider=_tokens
    )
    registry.register(
        name="tool_call_count", version="1.0.0", provider=_tool_calls
    )
    registry.register(
        name="evidence_utilization", version="1.0.0", provider=_evidence
    )
    registry.register(
        name="investigation_outcome", version="1.0.0", provider=_outcome
    )
