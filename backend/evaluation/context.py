"""EvaluationContext — evidence envelope propagated to every EvaluationStrategy."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from backend.evaluation.models import EvaluationSubject


@runtime_checkable
class EvaluationCancellationToken(Protocol):
    """Cooperative cancellation signal for evaluation strategies."""

    def is_set(self) -> bool:
        """Return ``True`` when cancellation has been requested."""
        ...

    async def wait(self) -> None:
        """Suspend until cancellation is requested."""
        ...


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    """Immutable evidence envelope passed to every EvaluationStrategy.

    ``evidence`` is the raw observation payload. Keys are domain-specific but
    callers should document what they supply so strategies can declare their
    requirements via ``evidence_refs`` in EvaluationResult.

    Common evidence keys (by convention, not enforced here):
        latency_ms, cost_usd, tokens_input, tokens_output,
        tool_results, workflow_status, agent_status,
        memory_bytes, retrieved_documents, ground_truth.
    """

    evaluation_id: str
    subject: EvaluationSubject
    subject_id: str
    evidence: dict[str, Any]
    subject_version: str | None = None
    workflow_id: str | None = None
    execution_id: str | None = None
    correlation_id: str | None = None
    tenant_id: str | None = None
    tags: tuple[str, ...] = ()
    deadline: datetime | None = None
    cancellation_token: EvaluationCancellationToken | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_cancelled(self) -> bool:
        """Return ``True`` when cancellation has been requested."""
        if self.cancellation_token is None:
            return False
        return self.cancellation_token.is_set()

    def has_timed_out(self, *, now: datetime | None = None) -> bool:
        """Return ``True`` when the deadline has elapsed."""
        if self.deadline is None:
            return False
        current = now or datetime.now(UTC)
        return current >= self.deadline

    def remaining_seconds(self, *, now: datetime | None = None) -> float | None:
        """Return remaining seconds until deadline, or ``None`` if no deadline."""
        if self.deadline is None:
            return None
        current = now or datetime.now(UTC)
        return max((self.deadline - current).total_seconds(), 0.0)

    def with_metadata(self, **kwargs: Any) -> EvaluationContext:
        """Return a copy with additional metadata merged in."""
        return replace(self, metadata={**self.metadata, **kwargs})
