"""EvidenceOrchestrator — connects EvidenceCollector to the incident lifecycle.

Responsibilities:
  1. Query every registered provider concurrently.
  2. Record individual provider failures without aborting collection.
  3. Emit lifecycle events (CollectionStarted / Collected /
     CollectionPartialFailure) through the RuntimeEventEmitter contract.
  4. Normalise the raw EvidenceCollection via EvidenceNormalizer.
  5. Return both the raw EvidenceCollection and the NormalizedEvidenceCollection
     so callers can choose their preferred representation.

The orchestrator is fully generic — it does not know about specific failure
modes, services, or cloud providers.  Partial evidence is preserved; the
investigation layer decides whether it is sufficient to continue.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import structlog

from backend.core.evidence_collector import EvidenceCollector
from backend.interfaces.evidence import EvidenceProvider
from backend.models.evidence import (
    Evidence,
    EvidenceCollection,
    EvidenceCorrelation,
    EvidenceSourceKind,
)
from backend.models.incident import Incident
from backend.services.evidence_normalizer import EvidenceNormalizer, NormalizedEvidenceCollection
from backend.services.incident_events import (
    EVIDENCE_COLLECTED,
    EVIDENCE_COLLECTION_PARTIAL_FAILURE,
    EVIDENCE_COLLECTION_STARTED,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Event emitter port — reuses the same structural contract as RuntimeEventEmitter
# ---------------------------------------------------------------------------


@runtime_checkable
class IncidentEventEmitter(Protocol):
    """Minimal event-emission contract for incident lifecycle events.

    Structurally compatible with ``RuntimeEventEmitter`` and
    ``WorkflowEventEmitter`` so any of those implementations can be injected.
    """

    async def emit(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: Any,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceOrchestrationResult:
    """Output of one evidence orchestration run.

    ``collection``        — raw EvidenceCollection (preserved for hypothesis engine)
    ``normalized``        — NormalizedEvidenceCollection (for agent / RCA)
    ``provider_errors``   — {provider_name: error_message} for failed providers
    ``partial``           — True when at least one provider failed
    ``duration_ms``       — wall-clock collection time
    """

    collection: EvidenceCollection
    normalized: NormalizedEvidenceCollection
    provider_errors: dict[str, str]
    partial: bool
    duration_ms: float


# ---------------------------------------------------------------------------
# EvidenceOrchestrator
# ---------------------------------------------------------------------------


@dataclass
class EvidenceOrchestrator:
    """Orchestrates evidence collection with lifecycle events and normalization.

    Inject:
      ``collector``      — EvidenceCollector with providers pre-registered, OR
      ``providers``      — list of EvidenceProvider added at construction time
      ``normalizer``     — EvidenceNormalizer (default: stateless singleton)
      ``event_emitter``  — IncidentEventEmitter (optional; no-op when None)
      ``max_concurrency``— semaphore width forwarded to each per-provider call
    """

    collector: EvidenceCollector | None = None
    providers: list[EvidenceProvider] = field(default_factory=list)
    normalizer: EvidenceNormalizer = field(default_factory=EvidenceNormalizer)
    event_emitter: IncidentEventEmitter | None = None
    max_concurrency: int = 10
    max_items_per_provider: int = 20

    def __post_init__(self) -> None:
        # Build a fresh collector if one wasn't injected, then register providers.
        if self.collector is None:
            self.collector = EvidenceCollector(
                max_concurrency=self.max_concurrency,
                max_items_per_provider=self.max_items_per_provider,
            )
        for p in self.providers:
            self.collector.register(p)

    def register(self, provider: EvidenceProvider) -> None:
        """Register an additional evidence provider at runtime."""
        assert self.collector is not None
        self.collector.register(provider)
        if provider not in self.providers:
            self.providers.append(provider)

    async def collect(
        self,
        incident: Incident,
        *,
        source_kinds: set[EvidenceSourceKind] | None = None,
        context: dict[str, Any] | None = None,
        emitter_context: Any = None,
    ) -> EvidenceOrchestrationResult:
        """Collect evidence for *incident*, emit lifecycle events, normalise.

        Args:
            incident:        The incident being investigated.
            source_kinds:    Optional filter — only query these provider kinds.
            context:         Forwarded to each provider's collect() call.
            emitter_context: Forwarded to event_emitter.emit() as third arg
                             (may be a RuntimeContext, WorkflowContext, or None).

        Returns:
            EvidenceOrchestrationResult with raw + normalised evidence,
            per-provider errors, and timing.
        """
        assert self.collector is not None
        bound_log = log.bind(
            incident_id=incident.incident_id,
            correlation_id=incident.correlation_id,
        )

        active_providers = [
            p for p in (self.collector.providers or [])
            if source_kinds is None or p.kind in source_kinds
        ]
        provider_names = [p.name for p in active_providers]

        await self._emit(
            EVIDENCE_COLLECTION_STARTED,
            {
                "incident_id": incident.incident_id,
                "correlation_id": incident.correlation_id,
                "provider_count": len(active_providers),
                "provider_names": provider_names,
                "source_kinds": [k.value for k in source_kinds] if source_kinds else None,
            },
            emitter_context,
        )
        bound_log.info(
            "evidence_collection_started",
            provider_count=len(active_providers),
            providers=provider_names,
        )

        t0 = time.monotonic()
        provider_errors: dict[str, str] = {}

        # Run each provider individually so we can attribute errors per-provider.
        # We bypass EvidenceCollector.collect() here to capture per-provider errors.
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def _collect_one(provider: EvidenceProvider) -> list[Evidence]:
            async with semaphore:
                try:
                    items = await provider.collect(
                        incident,
                        max_items=self.max_items_per_provider,
                        context=context,
                    )
                    bound_log.debug(
                        "provider_succeeded",
                        provider=provider.name,
                        item_count=len(items),
                    )
                    return items
                except Exception as exc:
                    error_msg = f"{type(exc).__name__}: {exc}"
                    provider_errors[provider.name] = error_msg
                    bound_log.warning(
                        "provider_failed",
                        provider=provider.name,
                        error=error_msg,
                    )
                    return []

        results = await asyncio.gather(*[_collect_one(p) for p in active_providers])

        all_items: list[Evidence] = []
        for batch in results:
            all_items.extend(batch)

        # Deduplicate and filter by relevance (mirrors EvidenceCollector internals)
        seen: set[str] = set()
        filtered: list[Evidence] = []
        for item in all_items:
            if (
                item.evidence_id not in seen
                and item.relevance_score >= self.collector.min_relevance_score
            ):
                seen.add(item.evidence_id)
                filtered.append(item)
        filtered.sort(key=lambda e: e.relevance_score, reverse=True)

        correlations: tuple[EvidenceCorrelation, ...] = self.collector._correlate(filtered)
        duration_ms = (time.monotonic() - t0) * 1000
        finished_at = datetime.now(UTC)

        collection = EvidenceCollection(
            incident_id=incident.incident_id,
            items=tuple(filtered),
            correlations=correlations,
            collected_at=finished_at,
            collection_duration_ms=duration_ms,
            sources_consulted=tuple(p.name for p in active_providers),
        )

        normalized = self.normalizer.normalize_collection(
            collection, provider_errors=provider_errors
        )

        partial = bool(provider_errors)
        event_name = (
            EVIDENCE_COLLECTION_PARTIAL_FAILURE if partial else EVIDENCE_COLLECTED
        )
        await self._emit(
            event_name,
            {
                "incident_id": incident.incident_id,
                "correlation_id": incident.correlation_id,
                "evidence_count": collection.count,
                "sources_consulted": list(collection.sources_consulted),
                "provider_errors": provider_errors,
                "partial": partial,
                "duration_ms": duration_ms,
            },
            emitter_context,
        )

        if partial:
            bound_log.warning(
                "evidence_collection_partial",
                evidence_count=collection.count,
                failed_providers=list(provider_errors.keys()),
                duration_ms=duration_ms,
            )
        else:
            bound_log.info(
                "evidence_collection_complete",
                evidence_count=collection.count,
                duration_ms=duration_ms,
            )

        return EvidenceOrchestrationResult(
            collection=collection,
            normalized=normalized,
            provider_errors=provider_errors,
            partial=partial,
            duration_ms=duration_ms,
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
