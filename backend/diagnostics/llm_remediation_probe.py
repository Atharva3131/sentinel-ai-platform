"""Probe the configured LLM RCA and GitHub remediation planner safely.

The probe follows the application's existing bootstrap path so Key Vault-backed
LLM credentials are resolved in the same place as normal dependency injection.
It deliberately stops after planning and never invokes the remediation engine,
GitHub executor, deployment pipeline, or verification coordinator.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from backend.application.container import ApplicationContainer
from backend.configuration.settings import get_settings
from backend.models.evidence import (
    Evidence,
    EvidenceCollection,
    EvidenceSource,
    EvidenceSourceKind,
    EvidenceStatus,
)
from backend.models.incident import Incident, IncidentSeverity, IncidentStatus
from backend.providers.github.planner import GitHubRemediationPlanner
from backend.providers.llm.factory import build_llm_provider
from backend.services.evidence_normalizer import EvidenceNormalizer
from backend.services.llm_investigation_agent import LLMInvestigationAgent


def _probe_incident() -> Incident:
    incident_id = f"llm-probe-{uuid.uuid4()}"
    now = datetime.now(UTC)
    return Incident(
        incident_id=incident_id,
        title="Sentinel LLM RCA probe",
        severity=IncidentSeverity.MEDIUM,
        status=IncidentStatus.OPEN,
        affected_services=("probe-service",),
        description="Synthetic read-only probe; no production service is targeted.",
        detected_at=now,
        symptoms=("elevated HTTP 5xx responses after a deployment",),
        correlation_id=incident_id,
    )


def _probe_evidence(incident: Incident) -> EvidenceCollection:
    now = datetime.now(UTC)
    evidence = Evidence(
        evidence_id=f"evidence-{uuid.uuid4()}",
        incident_id=incident.incident_id,
        source=EvidenceSource(
            source_id="sentinel-llm-probe",
            kind=EvidenceSourceKind.DEPLOYMENTS,
            name="synthetic-probe",
            collected_at=now,
        ),
        title="Recent deployment introduced elevated 5xx responses",
        content=(
            "Synthetic deployment evidence: version v2.4.1 was released immediately "
            "before the service error rate increased from 0.2% to 18%."
        ),
        status=EvidenceStatus.CONFIRMED,
        relevance_score=1.0,
        collected_at=now,
        structured_data={
            "service": "probe-service",
            "version": "v2.4.1",
            "error_rate_before": 0.002,
            "error_rate_after": 0.18,
        },
    )
    return EvidenceCollection(
        incident_id=incident.incident_id,
        items=(evidence,),
        correlations=(),
        collected_at=now,
        sources_consulted=("synthetic-probe",),
    )


def _validate_plan(plan: Any, incident: Incident, rca: Any) -> None:
    action_ids = [action.action_id for action in plan.actions]
    staged_ids = [action_id for stage in plan.stages for action_id in stage]
    if plan.incident_id != incident.incident_id:
        raise RuntimeError("Remediation plan incident_id does not match the probe incident")
    if plan.rca_id != rca.rca_id:
        raise RuntimeError("Remediation plan rca_id does not match the generated RCA")
    if not action_ids or len(action_ids) != len(set(action_ids)):
        raise RuntimeError("Remediation plan has no actions or duplicate action IDs")
    if set(action_ids) != set(staged_ids):
        raise RuntimeError("Remediation plan stages do not reference exactly its actions")


async def run_probe() -> dict[str, Any]:
    """Run the no-deployment probe and return a secret-free validation summary."""
    settings = get_settings()
    if settings.llm.primary.name.value == "fake":
        raise RuntimeError("The configured primary LLM provider is fake; use a real profile")
    if not settings.github.default_owner or not settings.github.default_repository:
        raise RuntimeError("GitHub default owner/repository must be configured for plan validation")

    container = await ApplicationContainer.build(settings)
    try:
        provider = build_llm_provider(container.settings.llm)
        incident = _probe_incident()
        evidence = _probe_evidence(incident)
        normalized = EvidenceNormalizer().normalize_collection(evidence)

        agent = LLMInvestigationAgent(
            llm_provider=provider,
            evidence_tool=None,
            max_tool_rounds=0,
        )
        agent_output = await agent.investigate(incident, evidence, normalized)
        rca = agent_output.rca
        if not rca.root_cause:
            raise RuntimeError(
                f"LLM returned an inconclusive RCA (confidence={rca.confidence:.3f})"
            )
        if not rca.root_cause.title.strip() or not rca.root_cause.description.strip():
            raise RuntimeError("LLM RCA is missing a root-cause title or description")

        planner = GitHubRemediationPlanner(
            owner=settings.github.default_owner,
            repo=settings.github.default_repository,
        )
        plan = await planner.plan(
            incident,
            rca,
            context={"branch_name": f"sentinel-probe/{incident.incident_id}"},
        )
        _validate_plan(plan, incident, rca)

        return {
            "provider": provider.name,
            "model": settings.llm.primary.model,
            "incident_id": incident.incident_id,
            "rca": {
                "rca_id": rca.rca_id,
                "root_cause_title": rca.root_cause.title,
                "root_cause_description": rca.root_cause.description,
                "confidence": rca.confidence,
                "supporting_evidence_ids": list(rca.root_cause.supporting_evidence_ids),
            },
            "remediation_plan": {
                "plan_id": plan.plan_id,
                "rca_id": plan.rca_id,
                "action_count": plan.action_count,
                "stage_count": len(plan.stages),
                "actions": [
                    {
                        "action_type": action.action_type.value,
                        "risk_level": action.risk_level.value,
                    }
                    for action in plan.actions
                ],
            },
            "deployment_executed": False,
        }
    finally:
        await container.close()


async def _main() -> None:
    print(json.dumps(await run_probe(), indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
