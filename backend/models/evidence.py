"""Evidence domain models — collected observations used for RCA."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class EvidenceSourceKind(StrEnum):
    """The category of system that supplied this evidence."""

    METRICS = "metrics"
    LOGS = "logs"
    TRACES = "traces"
    DEPLOYMENTS = "deployments"
    SOURCE_REPOSITORY = "source_repository"
    CONFIGURATION = "configuration"
    INFRASTRUCTURE = "infrastructure"
    HISTORICAL_INCIDENTS = "historical_incidents"
    KNOWLEDGE_BASE = "knowledge_base"
    SYNTHETIC = "synthetic"
    OTHER = "other"


class EvidenceStatus(StrEnum):
    """Reliability classification assigned after collection."""

    CONFIRMED = "confirmed"    # reliable, corroborated
    PROBABLE = "probable"      # likely correct, not fully corroborated
    SUSPECTED = "suspected"    # weak signal, needs further investigation
    CONTRADICTORY = "contradictory"  # conflicts with other evidence
    STALE = "stale"            # out-of-date, may not reflect current state
    UNAVAILABLE = "unavailable"  # collection failed


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    """Provenance descriptor for a piece of evidence.

    Identifies what system produced the data, when it was collected, and
    whether the data is authoritative.  The platform uses this to weight
    evidence during hypothesis evaluation.
    """

    source_id: str
    kind: EvidenceSourceKind
    name: str
    collected_at: datetime
    provider_version: str | None = None
    authoritative: bool = True
    latency_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Evidence:
    """A single piece of collected evidence relevant to an incident.

    ``content`` is the human-readable or structured representation of the
    observation.  ``structured_data`` holds machine-readable fields for
    downstream scoring and hypothesis evaluation.

    Evidence is incident-type agnostic — it does not assume a particular
    failure mode.  The RCA engine is responsible for interpreting relationships
    between evidence items.
    """

    evidence_id: str
    incident_id: str
    source: EvidenceSource
    title: str
    content: str
    status: EvidenceStatus
    relevance_score: float          # 0.0-1.0; set by the collection pipeline
    collected_at: datetime
    structured_data: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.relevance_score <= 1.0):
            raise ValueError(
                f"Evidence relevance_score must be 0.0-1.0, got {self.relevance_score}"
            )


@dataclass(frozen=True, slots=True)
class EvidenceCorrelation:
    """Relationship between two pieces of evidence.

    Captures whether evidence items support each other, contradict each
    other, or have an uncertain relationship.  The hypothesis engine uses
    these links when building confidence assessments.
    """

    correlation_id: str
    evidence_id_a: str
    evidence_id_b: str
    relationship: str           # "supports", "contradicts", "related", "temporal"
    strength: float             # 0.0-1.0
    explanation: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.strength <= 1.0):
            raise ValueError(
                f"EvidenceCorrelation strength must be 0.0-1.0, got {self.strength}"
            )


@dataclass(frozen=True, slots=True)
class EvidenceCollection:
    """All evidence gathered for one incident investigation phase.

    ``items`` is the ordered list of evidence by descending relevance.
    ``correlations`` captures cross-evidence relationships.
    ``collection_duration_ms`` measures the wall-clock cost of collection.
    """

    incident_id: str
    items: tuple[Evidence, ...]
    correlations: tuple[EvidenceCorrelation, ...]
    collected_at: datetime
    collection_duration_ms: float | None = None
    sources_consulted: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.items)

    def by_source_kind(self, kind: EvidenceSourceKind) -> tuple[Evidence, ...]:
        """Return evidence items from a specific source kind."""
        return tuple(e for e in self.items if e.source.kind == kind)

    def confirmed(self) -> tuple[Evidence, ...]:
        """Return only confirmed evidence items."""
        return tuple(e for e in self.items if e.status == EvidenceStatus.CONFIRMED)

    def top_k(self, k: int) -> tuple[Evidence, ...]:
        """Return the top-k evidence items by relevance score."""
        return tuple(sorted(self.items, key=lambda e: e.relevance_score, reverse=True)[:k])
