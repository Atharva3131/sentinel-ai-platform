"""Demo 1 evidence and observe-only safety tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from backend.configuration.settings import Demo1Settings
from backend.core.hypothesis_engine import DeterministicHypothesisGenerator, HypothesisEngine
from backend.core.remediation_engine import FakeActionExecutor, RemediationEngine
from backend.models.deployment import DeploymentState
from backend.models.evidence import EvidenceCollection
from backend.models.incident import (
    Incident,
    IncidentSeverity,
    IncidentSignal,
    IncidentStatus,
    SignalSource,
    SignalType,
)
from backend.models.remediation import (
    ActionRiskLevel,
    RemediationAction,
    RemediationActionType,
    RemediationPlan,
)
from backend.policies.action_policy import ActionPolicy, RemediationPolicyEngine
from backend.providers.evidence.demo1 import Demo1EvidenceProvider
from backend.providers.github.fake_deployment import FakeDeploymentProvider
from backend.services.closed_loop_orchestrator import ClosedLoopOrchestrator
from backend.services.deployment_pipeline import DeploymentPipeline
from backend.services.evidence_orchestrator import EvidenceOrchestrator
from backend.services.incident_repository import InMemoryIncidentRepository
from backend.services.investigation_orchestrator import InvestigationOrchestrator
from backend.services.llm_investigation_agent import LLMInvestigationAgent


def _now() -> datetime:
    return datetime.now(UTC)


def _incident(*, demo1: bool = True) -> Incident:
    return Incident(
        incident_id=str(uuid.uuid4()),
        title="Order API database connection pool exhausted",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        affected_services=("order-api",),
        description="Pool exhaustion detected.",
        detected_at=_now(),
        signals=(
            IncidentSignal(
                signal_id="signal-1",
                signal_type=SignalType.METRIC_THRESHOLD,
                source=SignalSource.OTHER,
                title="Pool exhausted",
                description="The pool has no free connections.",
                severity=IncidentSeverity.HIGH,
                received_at=_now(),
                service="order-api",
                environment="demo",
                raw_payload={"pool": {"max": 10, "free": 0, "used": 10}},
                labels={"failure_mode": "connection_leak"},
            ),
        ),
        symptoms=("Database connection pool exhausted",),
        environment="demo" if demo1 else "production",
        correlation_id=(
            "demo1-order-api-connection_leak-pool-exhausted" if demo1 else "other-incident"
        ),
        tags=("demo1", "database") if demo1 else ("database",),
        metadata={"image_version": "abc123", "pool": {"max": 10, "free": 0}},
    )


class _MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses: dict[str, tuple[int, dict[str, Any]]]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.responses[request.url.path]
        return httpx.Response(status, json=body)


def _wire(provider: Demo1EvidenceProvider, transport: httpx.AsyncBaseTransport) -> None:
    provider._http = httpx.AsyncClient(base_url=provider.base_url, transport=transport)


@pytest.mark.asyncio
async def test_demo1_provider_collects_health_and_metrics() -> None:
    provider = Demo1EvidenceProvider.from_settings(
        Demo1Settings(enabled=True, base_url="http://order-api")
    )
    transport = _MockTransport(
        {
            "/health": (
                200,
                {
                    "status": "DEGRADED",
                    "service": "order-api",
                    "version": "abc123",
                    "db_pool": {"max": 10, "free": 0, "used": 10},
                    "metrics": {"error_rate_pct": 18.0},
                },
            ),
            "/metrics/raw": (
                200,
                {
                    "service": "order-api",
                    "version": "abc123",
                    "pool": {"max": 10, "free": 0, "used": 10},
                    "failure_mode": "connection_leak",
                },
            ),
        }
    )
    _wire(provider, transport)

    evidence = await provider.collect(_incident())

    assert len(evidence) == 2
    assert {request.url.path for request in transport.requests} == {"/health", "/metrics/raw"}
    assert evidence[0].structured_data["version"] == "abc123"
    assert any(item.structured_data["pool"]["free"] == 0 for item in evidence)

    await provider.close()


@pytest.mark.asyncio
async def test_demo1_provider_returns_missing_evidence_when_unavailable() -> None:
    provider = Demo1EvidenceProvider.from_settings(
        Demo1Settings(enabled=True, base_url="http://order-api", max_retries=0)
    )
    transport = _MockTransport(
        {
            "/health": (503, {"error": "unavailable"}),
            "/metrics/raw": (503, {"error": "unavailable"}),
        }
    )
    _wire(provider, transport)

    assert await provider.collect(_incident()) == []
    assert await provider.health_check() is False
    await provider.close()


@pytest.mark.asyncio
async def test_demo1_provider_is_scoped_to_demo1_incidents() -> None:
    provider = Demo1EvidenceProvider.from_settings(
        Demo1Settings(enabled=True, base_url="http://order-api")
    )
    transport = _MockTransport({"/health": (200, {"service": "order-api"})})
    _wire(provider, transport)

    assert await provider.collect(_incident(demo1=False)) == []
    assert transport.requests == []
    await provider.close()


def test_llm_prompt_contains_incident_signal_and_metadata() -> None:
    agent = LLMInvestigationAgent(llm_provider=object())  # type: ignore[arg-type]
    collection = EvidenceCollection(
        incident_id="incident",
        items=(),
        correlations=(),
        collected_at=_now(),
    )
    normalized = EvidenceOrchestrator(providers=[]).normalizer.normalize_collection(collection)
    prompt = agent._build_evidence_prompt(_incident(), normalized)

    assert "connection_leak" in prompt
    assert "abc123" in prompt
    assert "free" in prompt


class _ExplodingExecutor:
    async def execute(self, action: Any, *, correlation_id: str | None = None) -> dict[str, Any]:
        raise AssertionError("real action executor was called")

    async def rollback(self, action: Any, *, correlation_id: str | None = None) -> dict[str, Any]:
        raise AssertionError("real action rollback was called")


class _ExplodingDeployment:
    async def trigger(self, request: Any, **kwargs: Any) -> Any:
        raise AssertionError("real deployment provider was called")


class _ExplodingAzure:
    async def should_remediate(self, incident: Incident) -> bool:
        raise AssertionError("Azure restart eligibility was checked")

    async def remediate(self, incident: Incident) -> Any:
        raise AssertionError("Azure restart was called")


class _VerificationMustNotRun:
    async def verify(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("verification must not report observe-only success")


async def _plan(incident: Incident, rca: Any) -> RemediationPlan:
    action = RemediationAction(
        action_id=str(uuid.uuid4()),
        action_type=RemediationActionType.CUSTOM,
        target_service="order-api",
        target_environment="demo",
        risk_level=ActionRiskLevel.LOW,
        title="observe",
        description="observe",
        parameters={},
    )
    return RemediationPlan(
        plan_id=str(uuid.uuid4()),
        incident_id=incident.incident_id,
        rca_id=rca.rca_id,
        actions=(action,),
        stages=((action.action_id,),),
        created_at=_now(),
    )


@pytest.mark.asyncio
async def test_demo1_observe_only_replaces_all_side_effecting_ports() -> None:
    policy = RemediationPolicyEngine(
        policy=ActionPolicy(
            auto_approve_risk_levels=frozenset(
                {
                    ActionRiskLevel.LOW,
                    ActionRiskLevel.MEDIUM,
                    ActionRiskLevel.HIGH,
                }
            )
        )
    )
    fake_actions = FakeActionExecutor()
    fake_deployment = FakeDeploymentProvider(final_state=DeploymentState.SUCCEEDED)
    demo_provider = Demo1EvidenceProvider.from_settings(
        Demo1Settings(enabled=True, base_url="http://order-api")
    )
    _wire(
        demo_provider,
        _MockTransport(
            {
                "/health": (
                    200,
                    {
                        "status": "DEGRADED",
                        "service": "order-api",
                        "version": "abc123",
                        "db_pool": {"max": 10, "free": 0, "used": 10},
                    },
                ),
                "/metrics/raw": (
                    200,
                    {
                        "service": "order-api",
                        "version": "abc123",
                        "pool": {"max": 10, "free": 0, "used": 10},
                    },
                ),
            }
        ),
    )
    investigation = InvestigationOrchestrator(
        evidence_orchestrator=EvidenceOrchestrator(providers=[demo_provider]),
        hypothesis_engine=HypothesisEngine(generator=DeterministicHypothesisGenerator()),
        max_iterations=1,
        confidence_threshold=0.0,
    )
    orchestrator = ClosedLoopOrchestrator(
        investigation=investigation,
        remediation_engine=RemediationEngine(
            policy_gateway=policy,
            executor=_ExplodingExecutor(),
        ),
        deployment_pipeline=DeploymentPipeline(
            provider=_ExplodingDeployment(),  # type: ignore[arg-type]
            policy_engine=policy,
        ),
        verification=_VerificationMustNotRun(),  # type: ignore[arg-type]
        repository=InMemoryIncidentRepository(),
        remediation_planner=_plan,
        azure_remediation_service=_ExplodingAzure(),
        deployment_owner="demo-owner",
        deployment_repo="demo-repo",
        observe_only_executor=fake_actions,
        observe_only_deployment_provider=fake_deployment,
        max_reinvestigation_cycles=1,
    )

    result = await orchestrator.run(_incident(), execution_id="observe-only-test")

    assert result.status == "escalated"
    assert result.resolved is False
    assert result.last_outcome is None
    assert len(fake_actions.executed) == 1
    assert len(fake_deployment.triggered) == 1

    await demo_provider.close()
