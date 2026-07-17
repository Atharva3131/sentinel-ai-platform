"""HybridRetriever — weighted fusion of vector and keyword retrieval results."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.retrieval.context import RetrievalContext
from backend.retrieval.models import RetrievalResult, RetrievalStrategy
from backend.retrieval.providers import RetrievalProvider


@dataclass(slots=True)
class HybridRetriever:
    """Combines a vector provider and a keyword provider into a single ranked list.

    Fusion algorithm:
        1. Run both providers concurrently (via asyncio.gather).
        2. For each unique chunk_id, compute a weighted linear combination:
               hybrid_score = vector_weight * vector_score
                            + keyword_weight * keyword_score
           A chunk appearing in only one provider gets score 0 for the other.
        3. Sort descending by hybrid_score.
        4. Apply ``context.min_score`` and ``context.top_k`` cutoffs.

    ``vector_weight`` and ``keyword_weight`` are automatically normalized to sum
    to 1.0 even if the caller provides non-normalized values.
    """

    vector_provider: RetrievalProvider
    keyword_provider: RetrievalProvider
    vector_weight: float = 0.7
    keyword_weight: float = 0.3
    _normalized: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        total = self.vector_weight + self.keyword_weight
        if total <= 0.0:
            raise ValueError(
                "vector_weight + keyword_weight must be positive, "
                f"got {total}"
            )
        object.__setattr__(self, "vector_weight", self.vector_weight / total)
        object.__setattr__(self, "keyword_weight", self.keyword_weight / total)
        object.__setattr__(self, "_normalized", True)

    @property
    def name(self) -> str:
        return "hybrid"

    @property
    def strategy(self) -> RetrievalStrategy:
        return RetrievalStrategy.HYBRID

    async def retrieve(
        self,
        context: RetrievalContext,
        embedding: list[float] | None = None,
    ) -> list[RetrievalResult]:
        """Return fused and ranked results from both underlying providers."""
        import asyncio

        vector_results, keyword_results = await asyncio.gather(
            self.vector_provider.retrieve(context, embedding),
            self.keyword_provider.retrieve(context, embedding=None),
            return_exceptions=True,
        )

        v_results: list[RetrievalResult] = (
            [] if isinstance(vector_results, BaseException) else vector_results
        )
        k_results: list[RetrievalResult] = (
            [] if isinstance(keyword_results, BaseException) else keyword_results
        )

        fused = self._fuse(v_results, k_results)
        filtered = [r for r in fused if r.score >= context.min_score]
        return filtered[: context.top_k]

    def _fuse(
        self,
        vector_results: list[RetrievalResult],
        keyword_results: list[RetrievalResult],
    ) -> list[RetrievalResult]:
        """Merge and re-score results from both providers by chunk_id."""
        vector_by_chunk: dict[str, RetrievalResult] = {
            r.chunk.chunk_id: r for r in vector_results
        }
        keyword_by_chunk: dict[str, RetrievalResult] = {
            r.chunk.chunk_id: r for r in keyword_results
        }
        all_chunk_ids = set(vector_by_chunk) | set(keyword_by_chunk)

        fused: list[RetrievalResult] = []
        for chunk_id in all_chunk_ids:
            v_score = vector_by_chunk[chunk_id].score if chunk_id in vector_by_chunk else 0.0
            k_score = keyword_by_chunk[chunk_id].score if chunk_id in keyword_by_chunk else 0.0
            hybrid_score = self.vector_weight * v_score + self.keyword_weight * k_score
            hybrid_score = min(max(hybrid_score, 0.0), 1.0)

            base = vector_by_chunk.get(chunk_id) or keyword_by_chunk[chunk_id]
            from dataclasses import replace

            fused.append(
                replace(
                    base,
                    score=hybrid_score,
                    metadata={
                        **base.metadata,
                        "vector_score": v_score,
                        "keyword_score": k_score,
                        "vector_weight": self.vector_weight,
                        "keyword_weight": self.keyword_weight,
                    },
                )
            )

        fused.sort(key=lambda r: r.score, reverse=True)
        return fused
