"""ContextOptimizer — deduplication, ranking, and token-budget trimming."""

from __future__ import annotations

from dataclasses import dataclass

from backend.retrieval.context import RetrievalContext
from backend.retrieval.exceptions import ContextOptimizationError
from backend.retrieval.models import RetrievalResult

_CHARS_PER_TOKEN: int = 4


def _estimate_tokens(text: str) -> int:
    """Approximate token count using the 4-chars-per-token heuristic."""
    return max(len(text) // _CHARS_PER_TOKEN, 1)


def _jaccard_similarity(a: str, b: str) -> float:
    """Character-trigram Jaccard similarity for near-duplicate detection."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    def trigrams(text: str) -> set[str]:
        return {text[i : i + 3] for i in range(len(text) - 2)}

    ta, tb = trigrams(a), trigrams(b)
    intersection = len(ta & tb)
    union = len(ta | tb)
    return intersection / union if union else 0.0


@dataclass(slots=True)
class ContextOptimizer:
    """Trims a list of RetrievalResults to fit within a token budget.

    Steps applied in order:
        1. Deduplicate — remove results whose content is near-identical to a
           higher-ranked result (trigram Jaccard >= ``dedup_threshold``).
        2. Sort — descending by score.
        3. Trim — drop results that push the cumulative token count past
           ``max_tokens`` (or ``context.max_tokens`` if set).
        4. Apply ``min_score`` filter.

    The optimizer is stateless and may be shared across concurrent pipeline runs.
    """

    max_tokens: int = 4096
    dedup_threshold: float = 0.92

    async def optimize(
        self,
        results: list[RetrievalResult],
        context: RetrievalContext,
    ) -> list[RetrievalResult]:
        """Return an ordered, deduplicated list of results within the token budget."""
        if not results:
            return []

        try:
            token_budget = context.max_tokens or self.max_tokens
            filtered = [r for r in results if r.score >= context.min_score]
            deduped = self._deduplicate(filtered)
            sorted_results = sorted(deduped, key=lambda r: r.score, reverse=True)
            trimmed = self._trim_to_budget(sorted_results, token_budget)
            return trimmed[: context.top_k]
        except Exception as exc:
            raise ContextOptimizationError(
                f"Context optimization failed: {exc}"
            ) from exc

    def _deduplicate(self, results: list[RetrievalResult]) -> list[RetrievalResult]:
        """Remove near-duplicate chunks; keep the higher-scored one."""
        sorted_by_score = sorted(results, key=lambda r: r.score, reverse=True)
        kept: list[RetrievalResult] = []
        for candidate in sorted_by_score:
            is_dupe = any(
                _jaccard_similarity(candidate.chunk.content, k.chunk.content)
                >= self.dedup_threshold
                for k in kept
            )
            if not is_dupe:
                kept.append(candidate)
        return kept

    def _trim_to_budget(
        self, results: list[RetrievalResult], max_tokens: int
    ) -> list[RetrievalResult]:
        """Return the longest prefix of results that fits within max_tokens."""
        total = 0
        trimmed: list[RetrievalResult] = []
        for result in results:
            tokens = _estimate_tokens(result.chunk.content)
            if total + tokens > max_tokens:
                break
            total += tokens
            trimmed.append(result)
        return trimmed
