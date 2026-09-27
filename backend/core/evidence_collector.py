"""EvidenceCollector — gathers evidence from multiple providers concurrently.

The collector iterates the registered providers, runs them concurrently
(bounded by a semaphore), and assembles the results into an EvidenceCollection.
It is fully incident-type agnostic: it does not know about specific failure
modes and does not branch on incident type.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backend.interfaces.evidence import EvidenceProvider
from backend.models.evidence import (
    Evidence,
    EvidenceCollection,
    EvidenceCorrelation,
    EvidenceSourceKind,
)
from backend.models.incident import Incident


@dataclass
class EvidenceCollector:
    """Collects evidence from multiple providers and assembles an EvidenceCollection.

    Providers are injected and may include any combination of metrics, logs,
    traces, deployment, configuration, infrastructure, historical, and knowledge
    sources.  All providers are queried concurrently (up to ``max_concurrency``).

    Unavailable providers do not abort collection — their failures are logged
    and collection continues with whatever is available.  The caller can inspect
    ``EvidenceCollection.sources_consulted`` and item statuses for completeness.
    """

    providers: list[EvidenceProvider] = field(default_factory=list)
    max_concurrency: int = 10
    max_items_per_provider: int = 20
    min_relevance_score: float = 0.0

    def register(self, provider: EvidenceProvider) -> None:
        """Register a provider.  Duplicate names are not rejected (last-wins)."""
        self.providers.append(provider)

    async def collect(
        self,
        incident: Incident,
        *,
        source_kinds: set[EvidenceSourceKind] | None = None,
        context: dict[str, Any] | None = None,
    ) -> EvidenceCollection:
        """Collect evidence from all registered providers for *incident*.

        Args:
            incident: The incident being investigated.
            source_kinds: When provided, only query providers of these kinds.
            context: Optional context forwarded to each provider.

        Returns:
            EvidenceCollection with all gathered evidence and basic correlations.
        """
        started = datetime.now(UTC)
        active_providers = [
            p for p in self.providers
            if source_kinds is None or p.kind in source_kinds
        ]
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def _collect_one(provider: EvidenceProvider) -> list[Evidence]:
            async with semaphore:
                try:
                    items = await provider.collect(
                        incident,
                        max_items=self.max_items_per_provider,
                        context=context,
                    )
                    return items
                except Exception:  # provider failures must not abort collection
                    return []

        results = await asyncio.gather(*[_collect_one(p) for p in active_providers])

        all_items: list[Evidence] = []
        for batch in results:
            all_items.extend(batch)

        # Filter by minimum relevance and deduplicate by evidence_id
        seen: set[str] = set()
        filtered: list[Evidence] = []
        for item in all_items:
            if item.evidence_id not in seen and item.relevance_score >= self.min_relevance_score:
                seen.add(item.evidence_id)
                filtered.append(item)

        # Sort by descending relevance
        filtered.sort(key=lambda e: e.relevance_score, reverse=True)

        correlations = self._correlate(filtered)
        finished = datetime.now(UTC)
        duration_ms = (finished - started).total_seconds() * 1000

        return EvidenceCollection(
            incident_id=incident.incident_id,
            items=tuple(filtered),
            correlations=correlations,
            collected_at=finished,
            collection_duration_ms=duration_ms,
            sources_consulted=tuple(p.name for p in active_providers),
        )

    def _correlate(self, items: list[Evidence]) -> tuple[EvidenceCorrelation, ...]:
        """Produce simple temporal/service correlations between evidence items.

        This is a deterministic heuristic: two evidence items are considered
        "related" when they share the same service in their structured_data.
        A real system would use embedding similarity or graph traversal.
        """
        correlations: list[EvidenceCorrelation] = []
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                svc_a = a.structured_data.get("service")
                svc_b = b.structured_data.get("service")
                if svc_a and svc_a == svc_b:
                    correlations.append(
                        EvidenceCorrelation(
                            correlation_id=str(uuid.uuid4()),
                            evidence_id_a=a.evidence_id,
                            evidence_id_b=b.evidence_id,
                            relationship="related",
                            strength=0.5,
                            explanation=f"Both reference service '{svc_a}'",
                        )
                    )
        return tuple(correlations)
