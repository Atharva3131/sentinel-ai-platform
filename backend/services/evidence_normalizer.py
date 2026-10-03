"""Evidence normalization — vendor-neutral representation layer.

The normalizer converts raw ``Evidence`` items (which carry provider-specific
``structured_data``) into ``NormalizedEvidence`` objects that every downstream
consumer (hypothesis engine, RCA, agent) can handle uniformly.

Design:
  * ``NormalizedEvidence`` is a frozen dataclass — safe to share across tasks.
  * The normalizer is stateless; it never talks to external services.
  * Fields are extracted from ``Evidence.structured_data`` using well-known
    keys, then from ``Evidence.source.metadata``, then from ``Evidence``
    itself.  Unknown keys are preserved in ``extra_metadata`` so no data is
    silently dropped.
  * The confidence/reliability field maps ``EvidenceStatus`` → float without
    hard-coding provider-specific logic.
  * No AWS/Azure/GitHub SDK is imported here or expected in ``structured_data``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backend.models.evidence import Evidence, EvidenceCollection, EvidenceStatus

# ---------------------------------------------------------------------------
# Status → reliability mapping
# ---------------------------------------------------------------------------

_STATUS_RELIABILITY: dict[EvidenceStatus, float] = {
    EvidenceStatus.CONFIRMED: 1.0,
    EvidenceStatus.PROBABLE: 0.75,
    EvidenceStatus.SUSPECTED: 0.5,
    EvidenceStatus.CONTRADICTORY: 0.25,
    EvidenceStatus.STALE: 0.1,
    EvidenceStatus.UNAVAILABLE: 0.0,
}


# ---------------------------------------------------------------------------
# NormalizedEvidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NormalizedEvidence:
    """Vendor-neutral evidence representation for cross-provider consumption.

    Every field that can vary per provider is extracted from the raw evidence
    into named, typed fields.  Downstream consumers MUST NOT branch on
    ``source`` values for business logic — they operate on the typed fields.

    Metadata contract:
      * ``source``       — provider name (stable identifier, not a URL/endpoint)
      * ``timestamp``    — when the observation was captured (UTC)
      * ``service``      — logical service name (e.g. "payment-service")
      * ``environment``  — deployment environment (e.g. "production", "staging")
      * ``resource``     — infrastructure resource identifier (optional)
      * ``version``      — deployed version / deployment tag (optional)
      * ``correlation_id`` — incident or trace correlation identifier
      * ``confidence``   — 0.0-1.0 reliability derived from EvidenceStatus
      * ``evidence_type`` — ``EvidenceSourceKind`` value string
      * ``extra_metadata`` — all remaining structured_data keys, preserved losslessly
    """

    evidence_id: str
    incident_id: str
    # ── provenance ──────────────────────────────────────────────────────────
    source: str
    evidence_type: str                          # EvidenceSourceKind value
    timestamp: datetime
    # ── primary identification ───────────────────────────────────────────────
    title: str
    content: str
    # ── context fields ───────────────────────────────────────────────────────
    service: str | None
    environment: str | None
    resource: str | None
    version: str | None
    correlation_id: str | None
    # ── scoring ──────────────────────────────────────────────────────────────
    relevance_score: float                      # 0.0-1.0 from Evidence
    confidence: float                           # 0.0-1.0 derived from EvidenceStatus
    # ── extension ────────────────────────────────────────────────────────────
    tags: tuple[str, ...] = ()
    extra_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.relevance_score <= 1.0):
            raise ValueError(f"relevance_score must be 0-1, got {self.relevance_score}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be 0-1, got {self.confidence}")


@dataclass(frozen=True, slots=True)
class NormalizedEvidenceCollection:
    """All normalized evidence for one investigation phase.

    ``items`` are ordered by descending relevance (same as the source
    ``EvidenceCollection``).  ``provider_errors`` captures any providers
    that failed — keyed by provider name, value is the error description.
    """

    incident_id: str
    items: tuple[NormalizedEvidence, ...]
    provider_errors: dict[str, str] = field(default_factory=dict)
    sources_consulted: tuple[str, ...] = ()
    collection_duration_ms: float | None = None

    @property
    def count(self) -> int:
        return len(self.items)

    def by_evidence_type(self, evidence_type: str) -> tuple[NormalizedEvidence, ...]:
        return tuple(e for e in self.items if e.evidence_type == evidence_type)

    def by_service(self, service: str) -> tuple[NormalizedEvidence, ...]:
        return tuple(e for e in self.items if e.service == service)

    def high_confidence(self, threshold: float = 0.6) -> tuple[NormalizedEvidence, ...]:
        return tuple(e for e in self.items if e.confidence >= threshold)


# ---------------------------------------------------------------------------
# EvidenceNormalizer
# ---------------------------------------------------------------------------

# Well-known keys in ``Evidence.structured_data`` that map to canonical fields.
# These are generic names — no vendor SDK keys.
_SERVICE_KEYS = ("service", "service_name", "svc", "app", "application")
_ENV_KEYS = ("environment", "env", "deployment_environment", "stage")
_RESOURCE_KEYS = ("resource", "resource_id", "resource_name", "instance", "pod", "node")
_VERSION_KEYS = ("version", "deployment_version", "image_tag", "release", "tag")
_CORRELATION_KEYS = ("correlation_id", "trace_id", "request_id", "incident_id")


def _first_match(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """Return the first non-empty string value found for any key in *keys*."""
    for k in keys:
        v = data.get(k)
        if isinstance(v, str) and v:
            return v
    return None


class EvidenceNormalizer:
    """Converts raw Evidence items into NormalizedEvidence.

    Stateless — safe to use as a module-level singleton or inject as a
    dependency.  Never contacts external services.
    """

    def normalize(self, evidence: Evidence) -> NormalizedEvidence:
        """Normalize a single Evidence item.

        Extraction order for each canonical field:
          1. ``evidence.structured_data`` — most specific
          2. ``evidence.source.metadata`` — provider-level context
          3. ``evidence`` top-level fields — fallback
        """
        sd = evidence.structured_data
        sm = evidence.source.metadata

        service: str | None = (
            _first_match(sd, _SERVICE_KEYS)
            or _first_match(sm, _SERVICE_KEYS)
        )
        environment: str | None = (
            _first_match(sd, _ENV_KEYS)
            or _first_match(sm, _ENV_KEYS)
        )
        resource: str | None = (
            _first_match(sd, _RESOURCE_KEYS)
            or _first_match(sm, _RESOURCE_KEYS)
        )
        version: str | None = (
            _first_match(sd, _VERSION_KEYS)
            or _first_match(sm, _VERSION_KEYS)
        )
        correlation_id: str | None = (
            _first_match(sd, _CORRELATION_KEYS)
            or _first_match(sm, _CORRELATION_KEYS)
        )

        confidence = _STATUS_RELIABILITY.get(evidence.status, 0.0)

        # Preserve all structured_data keys that were NOT consumed by canonical fields
        consumed_keys = {
            *_SERVICE_KEYS,
            *_ENV_KEYS,
            *_RESOURCE_KEYS,
            *_VERSION_KEYS,
            *_CORRELATION_KEYS,
        }
        extra = {k: v for k, v in sd.items() if k not in consumed_keys}

        return NormalizedEvidence(
            evidence_id=evidence.evidence_id,
            incident_id=evidence.incident_id,
            source=evidence.source.name,
            evidence_type=evidence.source.kind.value,
            timestamp=evidence.source.collected_at,
            title=evidence.title,
            content=evidence.content,
            service=service,
            environment=environment,
            resource=resource,
            version=version,
            correlation_id=correlation_id or evidence.incident_id,
            relevance_score=evidence.relevance_score,
            confidence=confidence,
            tags=evidence.tags,
            extra_metadata=extra,
        )

    def normalize_collection(
        self,
        collection: EvidenceCollection,
        *,
        provider_errors: dict[str, str] | None = None,
    ) -> NormalizedEvidenceCollection:
        """Normalize an entire EvidenceCollection.

        ``provider_errors`` carries the names and error descriptions of
        providers that failed during collection (recorded by the
        EvidenceOrchestrator).
        """
        items = tuple(self.normalize(e) for e in collection.items)
        return NormalizedEvidenceCollection(
            incident_id=collection.incident_id,
            items=items,
            provider_errors=provider_errors or {},
            sources_consulted=collection.sources_consulted,
            collection_duration_ms=collection.collection_duration_ms,
        )
