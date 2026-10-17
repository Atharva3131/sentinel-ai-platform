"""Probe the configured LLM RCA and GitHub remediation planner safely.

The probe follows the application's existing bootstrap path so Key Vault-backed
LLM credentials are resolved in the same place as normal dependency injection.
The default mode deliberately stops after planning. ``--dry-run`` continues
through policy and final-status handling using no-op action and deployment
providers; it never invokes the real GitHub executor, deployment provider, or
verification coordinator.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from backend.application.container import ApplicationContainer
from backend.configuration.settings import AppSettings, get_settings
from backend.core.remediation_engine import RemediationEngine
from backend.models.evidence import (
    Evidence,
    EvidenceCollection,
    EvidenceSource,
    EvidenceSourceKind,
    EvidenceStatus,
)
from backend.models.incident import Incident, IncidentSeverity, IncidentStatus
from backend.policies.action_policy import RemediationPolicyEngine
from backend.providers.github.fake_deployment import FakeDeploymentProvider
from backend.providers.github.planner import GitHubRemediationPlanner
from backend.providers.llm.factory import build_llm_provider
from backend.services.deployment_pipeline import DeploymentPipeline, build_deployment_request
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


class _DryRunActionExecutor:
    """Record approved actions without calling GitHub or another provider."""

    def __init__(self) -> None:
        self.executed: list[Any] = []
        self.rolled_back: list[Any] = []

    async def execute(self, action: Any, *, correlation_id: str | None = None) -> dict[str, Any]:
        self.executed.append(action)
        return {
            "status": "succeeded",
            "output": {"dry_run": True, "action_id": action.action_id},
            "error": None,
        }

    async def rollback(self, action: Any, *, correlation_id: str | None = None) -> dict[str, Any]:
        self.rolled_back.append(action)
        return {
            "status": "succeeded",
            "output": {"dry_run": True, "action_id": action.action_id},
            "error": None,
        }


def _policy_summary(policy: RemediationPolicyEngine, plan: Any) -> list[dict[str, Any]]:
    actions = {action.action_id: action for action in plan.actions}
    return [
        {
            "action_id": decision.action_id,
            "action_type": decision.action_type,
            "operation": actions[decision.action_id].metadata.get("github_operation"),
            "risk_level": actions[decision.action_id].risk_level.value,
            "allowed": decision.allowed,
            "reason": decision.reason,
        }
        for decision in policy.record.all()
    ]


async def _run_probe(
    settings: AppSettings,
    provider: Any,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Run the shared RCA/planning path, optionally through the safe dry-run path."""
    if settings.llm.primary.name.value == "fake" and not dry_run:
        raise RuntimeError("The configured primary LLM provider is fake; use a real profile")
    if not settings.github.default_owner or not settings.github.default_repository:
        raise RuntimeError("GitHub default owner/repository must be configured for plan validation")

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
        raise RuntimeError(f"LLM returned an inconclusive RCA (confidence={rca.confidence:.3f})")
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

    result: dict[str, Any] = {
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
                    "operation": action.metadata.get("github_operation"),
                }
                for action in plan.actions
            ],
        },
        "deployment_executed": False,
    }

    if not dry_run:
        return result

    # This path is deliberately assembled from no-network components. The real
    # policy is exercised, but approved actions are recorded by a no-op executor.
    policy = RemediationPolicyEngine()
    action_executor = _DryRunActionExecutor()
    remediation_result = await RemediationEngine(
        policy_gateway=policy,
        executor=action_executor,
    ).execute_plan(
        plan,
        incident,
        correlation_id=incident.correlation_id,
    )

    fake_deployment = FakeDeploymentProvider()
    deployment_result = None
    if remediation_result.succeeded:
        deployment_request = build_deployment_request(
            owner=settings.github.default_owner,
            repository=settings.github.default_repository,
            workflow_id=settings.deployment.workflow,
            ref="main",
            environment=settings.deployment.environment,
            incident_id=incident.incident_id,
            correlation_id=incident.correlation_id,
            deployment_id=f"dry-run-{incident.incident_id}",
        )
        deployment_result = await DeploymentPipeline(
            provider=fake_deployment,
            policy_engine=policy,
        ).deploy(deployment_request, incident)

    final_success = (
        remediation_result.succeeded
        and deployment_result is not None
        and deployment_result.succeeded
    )
    result.update(
        {
            "dry_run": True,
            "final_status": "resolved" if final_success else "failed",
            "incident_status": "resolved" if final_success else "escalated",
            "policy_decisions": _policy_summary(policy, plan),
            "remediation_result": {
                "succeeded": remediation_result.succeeded,
                "actions_attempted": remediation_result.actions_attempted,
                "actions_succeeded": remediation_result.actions_succeeded,
                "actions_failed": remediation_result.actions_failed,
                "policy_rejected_action_ids": remediation_result.metadata.get(
                    "policy_rejected_action_ids", []
                ),
                "action_results": remediation_result.action_results,
            },
            "deployment_executed": False,
            "deployment_provider_calls": len(fake_deployment.triggered),
            "azure_restart_attempted": False,
        }
    )
    return result


async def run_probe() -> dict[str, Any]:
    """Run the no-deployment probe and return a secret-free validation summary."""
    settings = get_settings()
    if settings.llm.primary.name.value == "fake":
        raise RuntimeError("The configured primary LLM provider is fake; use a real profile")
    container = await ApplicationContainer.build(settings)
    try:
        provider = build_llm_provider(container.settings.llm)
        return await _run_probe(container.settings, provider, dry_run=False)
    finally:
        await container.close()


async def run_dry_run() -> dict[str, Any]:
    """Run the complete no-side-effect RCA → policy → final-status path."""
    settings = get_settings()
    container = await ApplicationContainer.build(settings)
    try:
        provider = build_llm_provider(container.settings.llm)
        return await _run_probe(container.settings, provider, dry_run=True)
    finally:
        await container.close()


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run RCA, planning, policy, and final-status checks without external side effects",
    )
    args = parser.parse_args()
    probe = run_dry_run if args.dry_run else run_probe
    print(json.dumps(await probe(), indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
