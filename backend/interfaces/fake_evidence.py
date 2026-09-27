"""Fake / in-memory evidence provider implementations for testing.

These fakes satisfy the EvidenceProvider protocols without requiring any
external service.  They can be constructed with pre-configured responses to
drive deterministic tests.

None of these fakes contain hard-coded knowledge about specific failure modes.
They are configurable stubs — test suites inject the responses they need.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from backend.models.evidence import (
    Evidence,
    EvidenceSource,
    EvidenceSourceKind,
    EvidenceStatus,
)
from backend.models.incident import Incident


def _now() -> datetime:
    return datetime.now(UTC)


def _make_source(kind: EvidenceSourceKind, name: str) -> EvidenceSource:
    return EvidenceSource(
        source_id=str(uuid.uuid4()),
        kind=kind,
        name=name,
        collected_at=_now(),
        authoritative=True,
    )


def _make_evidence(
    incident_id: str,
    source: EvidenceSource,
    title: str,
    content: str,
    relevance_score: float = 0.8,
    structured_data: dict[str, Any] | None = None,
) -> Evidence:
    return Evidence(
        evidence_id=str(uuid.uuid4()),
        incident_id=incident_id,
        source=source,
        title=title,
        content=content,
        status=EvidenceStatus.CONFIRMED,
        relevance_score=relevance_score,
        collected_at=_now(),
        structured_data=structured_data or {},
    )


class FakeMetricsProvider:
    """Returns configurable metric observations."""

    kind = EvidenceSourceKind.METRICS
    name = "fake-metrics"

    def __init__(
        self,
        responses: list[Evidence] | None = None,
        *,
        healthy: bool = True,
    ) -> None:
        self._responses = responses or []
        self._healthy = healthy

    async def collect(
        self,
        incident: Incident,
        *,
        max_items: int = 20,
        context: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        if self._responses:
            return self._responses[:max_items]
        # Synthesise one observation per affected service
        source = _make_source(self.kind, self.name)
        return [
            _make_evidence(
                incident.incident_id,
                source,
                f"Metric anomaly: {svc}",
                f"Elevated error rate detected for {svc}",
                structured_data={"service": svc, "metric": "error_rate"},
            )
            for svc in incident.affected_services[:max_items]
        ]

    async def health_check(self) -> bool:
        return self._healthy

    async def query_metric(
        self,
        metric_name: str,
        service: str,
        *,
        window_seconds: float = 300.0,
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return {"metric": metric_name, "service": service, "value": 0.0, "fake": True}


class FakeLogsProvider:
    """Returns configurable log observations."""

    kind = EvidenceSourceKind.LOGS
    name = "fake-logs"

    def __init__(
        self,
        responses: list[Evidence] | None = None,
        log_lines: list[str] | None = None,
        *,
        healthy: bool = True,
    ) -> None:
        self._responses = responses or []
        self._log_lines = log_lines or []
        self._healthy = healthy

    async def collect(
        self,
        incident: Incident,
        *,
        max_items: int = 20,
        context: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        if self._responses:
            return self._responses[:max_items]
        source = _make_source(self.kind, self.name)
        return [
            _make_evidence(
                incident.incident_id,
                source,
                f"Log pattern: {svc}",
                f"Error log pattern detected in {svc} logs",
                structured_data={"service": svc},
            )
            for svc in incident.affected_services[:max_items]
        ]

    async def health_check(self) -> bool:
        return self._healthy

    async def search_logs(
        self,
        service: str,
        query: str,
        *,
        window_seconds: float = 300.0,
        max_lines: int = 500,
    ) -> list[str]:
        return self._log_lines[:max_lines]


class FakeTracesProvider:
    """Returns configurable trace observations."""

    kind = EvidenceSourceKind.TRACES
    name = "fake-traces"

    def __init__(
        self,
        responses: list[Evidence] | None = None,
        *,
        healthy: bool = True,
    ) -> None:
        self._responses = responses or []
        self._healthy = healthy

    async def collect(
        self,
        incident: Incident,
        *,
        max_items: int = 20,
        context: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        if self._responses:
            return self._responses[:max_items]
        source = _make_source(self.kind, self.name)
        return [
            _make_evidence(
                incident.incident_id,
                source,
                f"Trace anomaly: {svc}",
                f"Slow traces detected for {svc}",
                relevance_score=0.7,
                structured_data={"service": svc},
            )
            for svc in incident.affected_services[:max_items]
        ]

    async def health_check(self) -> bool:
        return self._healthy

    async def query_traces(
        self,
        service: str,
        *,
        window_seconds: float = 300.0,
        error_only: bool = False,
        max_traces: int = 50,
    ) -> list[dict[str, Any]]:
        return [{"service": service, "fake": True}][:max_traces]


class FakeDeploymentProvider:
    """Returns configurable deployment history evidence."""

    kind = EvidenceSourceKind.DEPLOYMENTS
    name = "fake-deployments"

    def __init__(
        self,
        responses: list[Evidence] | None = None,
        deployments: list[dict[str, Any]] | None = None,
        *,
        healthy: bool = True,
    ) -> None:
        self._responses = responses or []
        self._deployments = deployments or []
        self._healthy = healthy

    async def collect(
        self,
        incident: Incident,
        *,
        max_items: int = 20,
        context: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        if self._responses:
            return self._responses[:max_items]
        if not self._deployments:
            return []
        source = _make_source(self.kind, self.name)
        return [
            _make_evidence(
                incident.incident_id,
                source,
                f"Recent deployment: {d.get('service', 'unknown')}",
                f"Deployment {d.get('version', '?')} at {d.get('deployed_at', '?')}",
                relevance_score=0.9,
                structured_data=d,
            )
            for d in self._deployments[:max_items]
        ]

    async def health_check(self) -> bool:
        return self._healthy

    async def recent_deployments(
        self,
        service: str,
        *,
        window_seconds: float = 86_400.0,
        max_deployments: int = 10,
    ) -> list[dict[str, Any]]:
        return [d for d in self._deployments if d.get("service") == service][:max_deployments]


class FakeConfigurationProvider:
    """Returns configurable configuration evidence."""

    kind = EvidenceSourceKind.CONFIGURATION
    name = "fake-configuration"

    def __init__(
        self,
        responses: list[Evidence] | None = None,
        configs: dict[str, dict[str, Any]] | None = None,
        changes: list[dict[str, Any]] | None = None,
        *,
        healthy: bool = True,
    ) -> None:
        self._responses = responses or []
        self._configs = configs or {}
        self._changes = changes or []
        self._healthy = healthy

    async def collect(
        self,
        incident: Incident,
        *,
        max_items: int = 20,
        context: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        if self._responses:
            return self._responses[:max_items]
        return []

    async def health_check(self) -> bool:
        return self._healthy

    async def current_config(
        self,
        service: str,
        *,
        include_secrets: bool = False,
    ) -> dict[str, Any]:
        return self._configs.get(service, {})

    async def config_changes(
        self,
        service: str,
        *,
        window_seconds: float = 86_400.0,
    ) -> list[dict[str, Any]]:
        return [c for c in self._changes if c.get("service") == service]


class FakeInfrastructureProvider:
    """Returns configurable infrastructure state evidence."""

    kind = EvidenceSourceKind.INFRASTRUCTURE
    name = "fake-infrastructure"

    def __init__(
        self,
        responses: list[Evidence] | None = None,
        utilization: dict[str, dict[str, float]] | None = None,
        *,
        healthy: bool = True,
    ) -> None:
        self._responses = responses or []
        self._utilization = utilization or {}
        self._healthy = healthy

    async def collect(
        self,
        incident: Incident,
        *,
        max_items: int = 20,
        context: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        if self._responses:
            return self._responses[:max_items]
        return []

    async def health_check(self) -> bool:
        return self._healthy

    async def resource_utilization(
        self,
        service: str,
        *,
        window_seconds: float = 300.0,
    ) -> dict[str, float]:
        return self._utilization.get(service, {"cpu": 0.0, "memory": 0.0})


class FakeHistoricalIncidentsProvider:
    """Returns configurable historical incident evidence."""

    kind = EvidenceSourceKind.HISTORICAL_INCIDENTS
    name = "fake-historical-incidents"

    def __init__(
        self,
        responses: list[Evidence] | None = None,
        similar: list[dict[str, Any]] | None = None,
        *,
        healthy: bool = True,
    ) -> None:
        self._responses = responses or []
        self._similar = similar or []
        self._healthy = healthy

    async def collect(
        self,
        incident: Incident,
        *,
        max_items: int = 20,
        context: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        if self._responses:
            return self._responses[:max_items]
        if not self._similar:
            return []
        source = _make_source(self.kind, self.name)
        return [
            _make_evidence(
                incident.incident_id,
                source,
                f"Historical incident: {s.get('title', 'unknown')}",
                f"Similar past incident: {s.get('summary', '')}",
                relevance_score=float(s.get("similarity", 0.6)),
                structured_data=s,
            )
            for s in self._similar[:max_items]
        ]

    async def health_check(self) -> bool:
        return self._healthy

    async def similar_incidents(
        self,
        incident: Incident,
        *,
        max_results: int = 10,
        min_similarity: float = 0.5,
    ) -> list[dict[str, Any]]:
        return [
            s for s in self._similar
            if float(s.get("similarity", 0.0)) >= min_similarity
        ][:max_results]


class FakeKnowledgeBaseProvider:
    """Returns configurable knowledge-base / runbook evidence."""

    kind = EvidenceSourceKind.KNOWLEDGE_BASE
    name = "fake-knowledge-base"

    def __init__(
        self,
        responses: list[Evidence] | None = None,
        runbooks: list[dict[str, Any]] | None = None,
        *,
        healthy: bool = True,
    ) -> None:
        self._responses = responses or []
        self._runbooks = runbooks or []
        self._healthy = healthy

    async def collect(
        self,
        incident: Incident,
        *,
        max_items: int = 20,
        context: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        if self._responses:
            return self._responses[:max_items]
        if not self._runbooks:
            return []
        source = _make_source(self.kind, self.name)
        return [
            _make_evidence(
                incident.incident_id,
                source,
                f"Runbook: {r.get('title', 'unknown')}",
                r.get("content", ""),
                relevance_score=float(r.get("score", 0.7)),
                structured_data=r,
            )
            for r in self._runbooks[:max_items]
        ]

    async def health_check(self) -> bool:
        return self._healthy

    async def retrieve_runbooks(
        self,
        incident: Incident,
        *,
        max_results: int = 5,
    ) -> list[dict[str, Any]]:
        return self._runbooks[:max_results]
