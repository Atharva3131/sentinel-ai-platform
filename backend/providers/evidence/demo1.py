"""Evidence adapter for the external Demo 1 Order API."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from backend.configuration.settings import Demo1Settings
from backend.models.evidence import (
    Evidence,
    EvidenceSource,
    EvidenceSourceKind,
    EvidenceStatus,
)
from backend.models.incident import Incident
from backend.providers.evidence.base import EvidenceHttpBase
from backend.services.demo1 import is_demo1_incident

log = structlog.get_logger(__name__)


@dataclass
class Demo1EvidenceProvider(EvidenceHttpBase):
    """Collect health and raw pool metrics from Demo 1.

    Failed or unavailable requests return no evidence.  The investigation
    pipeline therefore sees the source as missing rather than receiving a
    fabricated healthy observation.
    """

    enabled: bool = True

    @classmethod
    def from_settings(cls, settings: Demo1Settings) -> Demo1EvidenceProvider:
        provider = cls(
            provider_name="demo1-order-api",
            base_url=settings.base_url.rstrip("/"),
            timeout_seconds=settings.timeout_seconds,
            max_retries=settings.max_retries,
            retry_min_wait=settings.retry_min_wait_seconds,
            retry_max_wait=settings.retry_max_wait_seconds,
            enabled=settings.enabled,
        )
        return provider

    @property
    def kind(self) -> EvidenceSourceKind:
        return EvidenceSourceKind.METRICS

    @property
    def name(self) -> str:
        return "demo1-order-api"

    async def collect(
        self,
        incident: Incident,
        *,
        max_items: int = 20,
        context: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        if not is_demo1_incident(incident) or not self.enabled or not self.base_url:
            return []

        collected: list[Evidence] = []
        for path, title in (
            ("/health", "Demo 1 health status"),
            ("/metrics/raw", "Demo 1 raw metrics and pool status"),
        ):
            if len(collected) >= max_items:
                break
            try:
                payload = await self._get(
                    path,
                    span_name=f"evidence.demo1.{path.strip('/').replace('/', '_')}",
                    incident_id=incident.incident_id,
                    correlation_id=incident.correlation_id,
                )
            except Exception as exc:
                log.warning(
                    "demo1_evidence_unavailable",
                    endpoint=path,
                    incident_id=incident.incident_id,
                    error=str(exc),
                )
                continue

            now = datetime.now(UTC)
            collected.append(
                Evidence(
                    evidence_id=str(uuid.uuid4()),
                    incident_id=incident.incident_id,
                    source=EvidenceSource(
                        source_id=str(uuid.uuid4()),
                        kind=self.kind,
                        name=self.name,
                        collected_at=now,
                        authoritative=True,
                        metadata={"endpoint": path},
                    ),
                    title=title,
                    content=json.dumps(payload, sort_keys=True, default=str),
                    status=EvidenceStatus.CONFIRMED,
                    relevance_score=0.95 if path == "/health" else 0.9,
                    collected_at=now,
                    structured_data={
                        "service": payload.get("service", "order-api"),
                        "version": payload.get("version"),
                        "failure_mode": payload.get("failure_mode"),
                        "pool": payload.get("db_pool", payload.get("pool", {})),
                        "metrics": payload.get("metrics", {}),
                        "status": payload.get("status"),
                        "endpoint": path,
                    },
                )
            )
        return collected

    async def health_check(self) -> bool:
        if not self.enabled or not self.base_url:
            return False
        try:
            payload = await self._get("/health", span_name="evidence.demo1.health")
            return bool(payload.get("service"))
        except Exception:
            return False
