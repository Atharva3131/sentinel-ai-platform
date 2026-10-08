"""Closed-loop verification and reinvestigation tests — all 20 scenarios.

Tests exercise the REAL orchestration path using fakes for every external
dependency.  No live infrastructure required.

Scenarios:
 1.  Successful deployment → verification pass → resolved
 2.  Verification observation window (waited before evidence collection)
 3.  Before/after evidence collection
 4.  Verification pass
 5.  Verification failure
 6.  Failure → reinvestigation cycle
 7.  Revised remediation after failed verification
 8.  Maximum reinvestigation limit → escalated
 9.  Deployment failure stops loop
10.  Verification timeout → reinvestigation
11.  Verification cancellation
12.  Evidence-provider partial failure during verification
13.  Duplicate/idempotent: already-resolved incident skipped
14.  Idempotent retry after process interruption
15.  Incident resolution persisted to repository
16.  Audit event emission (all expected events present)
17.  OTel/correlation propagation through events
18.  Complete mocked closed-loop: incident → resolved (pass path)
19.  Closed-loop failure → reinvestigation → resolved
20.  Closed-loop max-cycles escalation
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from backend.core.hypothesis_engine import DeterministicHypothesisGenerator, HypothesisEngine
from backend.core.remediation_engine import (
    AlwaysAllowPolicyGateway,
    FakeActionExecutor,
    RemediationEngine,
)
from backend.core.validation_engine import FakeMetricsSnapshot, VerificationEngine
from backend.interfaces.fake_evidence import FakeMetricsProvider
from backend.models.deployment import (
    DeploymentRequest,
    DeploymentResult,
    DeploymentState,
    DeploymentStatus,
)
from backend.models.incident import Incident, IncidentSeverity, IncidentStatus
from backend.models.remediation import (
    ActionRiskLevel,
    RemediationAction,
    RemediationActionType,
    RemediationPlan,
)
from backend.models.resolution import ResolutionStatus
from backend.models.validation import VerificationPlan
from backend.policies.action_policy import ActionPolicy, RemediationPolicyEngine
from backend.providers.github.fake_deployment import FakeDeploymentProvider
from backend.services.closed_loop_orchestrator import (
    ClosedLoopOrchestrator,
    ClosedLoopResult,
    _default_verification_plan,
)
from backend.services.deployment_pipeline import DeploymentPipeline
from backend.services.evidence_orchestrator import EvidenceOrchestrator
from backend.services.incident_events import (
    INCIDENT_ESCALATED,
    INCIDENT_RESOLVED,
    REINVESTIGATION_LIMIT_REACHED,
    REINVESTIGATION_STARTED,
    VERIFICATION_COMPLETED,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    VERIFICATION_STARTED,
    VERIFICATION_TIMED_OUT,
)
from backend.services.incident_repository import InMemoryIncidentRepository
from backend.services.investigation_orchestrator import InvestigationOrchestrator
from backend.services.verification_coordinator import (
    VerificationCoordinator,
    VerificationState,
)

# ── Shared helpers ────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _incident(
    *,
    inc_id: str | None = None,
    status: IncidentStatus = IncidentStatus.OPEN,
) -> Incident:
    return Incident(
        incident_id=inc_id or str(uuid.uuid4()),
        title="Test incident",
        severity=IncidentSeverity.HIGH,
        status=status,
        affected_services=("svc-a",),
        description="Test.",
        detected_at=_now(),
        symptoms=("elevated error rate",),
        correlation_id=str(uuid.uuid4()),
    )


def _deploy_result(
    *,
    state: DeploymentState = DeploymentState.SUCCEEDED,
    incident_id: str = "inc-test",
) -> DeploymentResult:
    req = DeploymentRequest(
        deployment_id=str(uuid.uuid4()),
        owner="my-org",
        repository="my-repo",
        workflow_id="deploy.yml",
        environment="staging",
        ref="main",
        incident_id=incident_id,
    )
    return DeploymentResult(
        request=req,
        status=DeploymentStatus(
            deployment_id=req.deployment_id,
            run_id="run-001",
            state=state,
            environment="staging",
        ),
        error=None if state == DeploymentState.SUCCEEDED else "deployment failed",
    )


class _EventCollector:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, name: str, payload: Mapping[str, Any], ctx: Any) -> None:
        self.events.append((name, dict(payload)))

    def names(self) -> list[str]:
        return [e[0] for e in self.events]

    def payloads_for(self, name: str) -> list[dict[str, Any]]:
        return [p for n, p in self.events if n == name]


def _make_investigation(
    *,
    providers: list[Any] | None = None,
    emitter: Any = None,
    confidence_threshold: float = 0.0,
) -> InvestigationOrchestrator:
    gen = DeterministicHypothesisGenerator()
    engine = HypothesisEngine(generator=gen)
    orchestrator = EvidenceOrchestrator(
        providers=providers or [FakeMetricsProvider()],
        event_emitter=emitter,
    )
    return InvestigationOrchestrator(
        evidence_orchestrator=orchestrator,
        hypothesis_engine=engine,
        event_emitter=emitter,
        max_iterations=1,
        confidence_threshold=confidence_threshold,
    )


def _make_remediation_engine() -> RemediationEngine:
    return RemediationEngine(
        policy_gateway=AlwaysAllowPolicyGateway(),
        executor=FakeActionExecutor(default_succeed=True),
    )


async def _noop_planner(incident: Incident, rca: Any) -> RemediationPlan:
    action = RemediationAction(
        action_id=str(uuid.uuid4()),
        action_type=RemediationActionType.CUSTOM,
        target_service="svc-a",
        target_environment="staging",
        risk_level=ActionRiskLevel.LOW,
        title="noop",
        description="",
        parameters={},
    )
    return RemediationPlan(
        plan_id=str(uuid.uuid4()),
        incident_id=incident.incident_id,
        rca_id=None,
        actions=(action,),
        stages=((action.action_id,),),
        created_at=_now(),
    )


def _make_deployment_pipeline(
    *,
    final_state: DeploymentState = DeploymentState.SUCCEEDED,
    emitter: Any = None,
) -> DeploymentPipeline:
    policy = RemediationPolicyEngine(
        policy=ActionPolicy(
            auto_approve_risk_levels=frozenset({
                ActionRiskLevel.LOW, ActionRiskLevel.MEDIUM, ActionRiskLevel.HIGH,
            })
        )
    )
    fake = FakeDeploymentProvider(final_state=final_state)
    return DeploymentPipeline(
        provider=fake,
        policy_engine=policy,
        event_emitter=emitter,
    )


def _make_verification_coordinator(
    *,
    before_values: dict[str, float] | None = None,
    after_values: dict[str, float] | None = None,
    emitter: Any = None,
    observation_window: float = 0.0,  # zero for fast tests
) -> VerificationCoordinator:
    """Build a VerificationCoordinator whose engine will pass or fail based on values."""
    after = after_values or {"error_rate": 0.01}

    snapshot = FakeMetricsSnapshot(
        values={"svc-a": after}
    )
    engine = VerificationEngine(metrics_snapshot=snapshot)
    orchestrator = EvidenceOrchestrator(providers=[FakeMetricsProvider()])
    return VerificationCoordinator(
        verification_engine=engine,
        evidence_orchestrator=orchestrator,
        event_emitter=emitter,
        observation_window_seconds=observation_window,
        verification_timeout_seconds=30.0,
    )


def _make_verification_plan(
    incident_id: str,
    *,
    before_values: dict[str, float] | None = None,
    threshold: float = 0.05,
) -> VerificationPlan:
    before = before_values or {"error_rate": 0.15}
    return VerificationPlan(
        plan_id=str(uuid.uuid4()),
        incident_id=incident_id,
        remediation_plan_id="",
        metrics_to_compare=("error_rate",),
        comparison_window_seconds=10.0,
        thresholds={"error_rate": threshold},
        snapshot_before={"svc-a": before},
    )


# ── 1. Successful deployment → verification pass → resolved ───────────────────


@pytest.mark.asyncio
async def test_verification_coordinator_passes_when_metrics_improve() -> None:
    """Verification passes when after-metrics are below threshold."""
    emitter = _EventCollector()
    coordinator = _make_verification_coordinator(
        after_values={"error_rate": 0.01},  # below 0.05 threshold
        emitter=emitter,
    )
    inc = _incident()
    plan = _make_verification_plan(inc.incident_id, before_values={"error_rate": 0.15})
    deploy = _deploy_result(incident_id=inc.incident_id)

    outcome = await coordinator.verify(inc, plan, deploy)

    assert outcome.passed
    assert outcome.state == VerificationState.PASSED
    assert not outcome.reinvestigate
    assert VERIFICATION_PASSED in emitter.names()
    assert VERIFICATION_COMPLETED in emitter.names()


# ── 2. Verification observation window ───────────────────────────────────────


@pytest.mark.asyncio
async def test_verification_waits_observation_window() -> None:
    """Coordinator emits ObservationStarted and waits before collecting."""
    import time

    from backend.services.incident_events import VERIFICATION_OBSERVATION_STARTED

    emitter = _EventCollector()
    coordinator = _make_verification_coordinator(
        after_values={"error_rate": 0.01},
        emitter=emitter,
        observation_window=0.05,  # 50 ms
    )
    inc = _incident()
    plan = _make_verification_plan(inc.incident_id)
    deploy = _deploy_result(incident_id=inc.incident_id)

    t0 = time.monotonic()
    await coordinator.verify(inc, plan, deploy)
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.04   # waited at least the window
    assert VERIFICATION_OBSERVATION_STARTED in emitter.names()


# ── 3. Before/after evidence collection ──────────────────────────────────────


@pytest.mark.asyncio
async def test_verification_collects_evidence_after_window() -> None:
    """VerificationCoordinator collects evidence and includes in outcome."""
    from backend.services.incident_events import VERIFICATION_EVIDENCE_COLLECTED

    emitter = _EventCollector()
    coordinator = _make_verification_coordinator(emitter=emitter)
    inc = _incident()
    plan = _make_verification_plan(inc.incident_id)
    deploy = _deploy_result(incident_id=inc.incident_id)

    outcome = await coordinator.verify(inc, plan, deploy)

    assert VERIFICATION_EVIDENCE_COLLECTED in emitter.names()
    # verification_result carries before/after comparison
    assert outcome.verification_result is not None
    assert len(outcome.verification_result.comparisons) > 0


# ── 4. Verification pass ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verification_coordinator_pass_path() -> None:
    """PASSED outcome has reinvestigate=False and verification_result."""
    coordinator = _make_verification_coordinator(after_values={"error_rate": 0.01})
    inc = _incident()
    plan = _make_verification_plan(inc.incident_id)

    outcome = await coordinator.verify(inc, plan, _deploy_result(incident_id=inc.incident_id))

    assert outcome.passed
    assert outcome.reinvestigate is False
    assert outcome.state == VerificationState.PASSED
    assert outcome.verification_result is not None


# ── 5. Verification failure ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verification_coordinator_fail_when_metrics_degraded() -> None:
    """Verification fails when after-metrics exceed threshold."""
    emitter = _EventCollector()
    coordinator = _make_verification_coordinator(
        after_values={"error_rate": 0.30},  # above 0.05 threshold
        emitter=emitter,
    )
    inc = _incident()
    plan = _make_verification_plan(inc.incident_id, before_values={"error_rate": 0.01})

    outcome = await coordinator.verify(inc, plan, _deploy_result(incident_id=inc.incident_id))

    assert not outcome.passed
    assert outcome.state == VerificationState.FAILED
    assert outcome.reinvestigate is True
    assert VERIFICATION_FAILED in emitter.names()


# ── 6. Failure → reinvestigation cycle ───────────────────────────────────────


@pytest.mark.asyncio
async def test_closed_loop_reinvestigates_on_first_verification_failure() -> None:
    """ClosedLoopOrchestrator enters a second cycle when first verification fails."""
    emitter = _EventCollector()
    repo = InMemoryIncidentRepository()
    inc = _incident()

    call_count = [0]

    async def _planner(incident: Incident, rca: Any) -> VerificationPlan:
        call_count[0] += 1
        # Patch coordinator's engine (unused variable removed)
        return _make_verification_plan(incident.incident_id)

    # Verification coordinator that fails on cycle 1, passes on cycle 2
    cycle_count = [0]

    class _FlipCoordinator(VerificationCoordinator):
        async def verify(self, incident: Incident, plan: Any, dep: Any, **kw: Any) -> Any:  # type: ignore[override]
            cycle_count[0] += 1
            if cycle_count[0] == 1:
                # Fail first
                from backend.services.verification_coordinator import VerificationOutcome
                return VerificationOutcome(
                    state=VerificationState.FAILED,
                    incident_id=incident.incident_id,
                    deployment_id=dep.request.deployment_id if dep else None,
                    verification_result=None,
                    observation_duration_ms=0.0,
                    total_duration_ms=0.0,
                    reinvestigate=True,
                    failure_reason="First cycle failed",
                )
            else:
                # Pass second
                from backend.services.verification_coordinator import VerificationOutcome
                return VerificationOutcome(
                    state=VerificationState.PASSED,
                    incident_id=incident.incident_id,
                    deployment_id=dep.request.deployment_id if dep else None,
                    verification_result=None,
                    observation_duration_ms=0.0,
                    total_duration_ms=0.0,
                    reinvestigate=False,
                )

    snapshot = FakeMetricsSnapshot(values={"svc-a": {"error_rate": 0.01}})
    engine = VerificationEngine(metrics_snapshot=snapshot)
    orch = EvidenceOrchestrator(providers=[FakeMetricsProvider()])
    flip_coordinator = _FlipCoordinator(
        verification_engine=engine,
        evidence_orchestrator=orch,
        event_emitter=emitter,
        observation_window_seconds=0.0,
    )

    orchestrator = ClosedLoopOrchestrator(
        investigation=_make_investigation(emitter=emitter),
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=_make_deployment_pipeline(emitter=emitter),
        verification=flip_coordinator,
        repository=repo,
        remediation_planner=_noop_planner,
        event_emitter=emitter,
        max_reinvestigation_cycles=3,
        deployment_owner="org",
        deployment_repo="repo",
    )

    result = await orchestrator.run(inc)

    assert result.resolved
    assert result.cycles == 2
    assert REINVESTIGATION_STARTED in emitter.names()
    assert INCIDENT_RESOLVED in emitter.names()


# ── 7. Revised remediation after failed verification ─────────────────────────


@pytest.mark.asyncio
async def test_closed_loop_runs_remediation_on_each_reinvestigation_cycle() -> None:
    """Remediation planner is called on every cycle."""
    repo = InMemoryIncidentRepository()
    inc = _incident()
    planner_calls: list[int] = []
    cycle_count = [0]

    async def _counting_planner(incident: Incident, rca: Any) -> RemediationPlan:
        planner_calls.append(len(planner_calls) + 1)
        return await _noop_planner(incident, rca)

    class _FlipCoordinator(VerificationCoordinator):
        async def verify(self, incident: Incident, plan: Any, dep: Any, **kw: Any) -> Any:  # type: ignore[override]
            from backend.services.verification_coordinator import VerificationOutcome
            cycle_count[0] += 1
            passed = cycle_count[0] >= 2
            return VerificationOutcome(
                state=VerificationState.PASSED if passed else VerificationState.FAILED,
                incident_id=incident.incident_id,
                deployment_id=None,
                verification_result=None,
                observation_duration_ms=0.0,
                total_duration_ms=0.0,
                reinvestigate=not passed,
            )

    snapshot = FakeMetricsSnapshot(values={"svc-a": {"error_rate": 0.01}})
    engine = VerificationEngine(metrics_snapshot=snapshot)
    orch_ev = EvidenceOrchestrator(providers=[FakeMetricsProvider()])
    coordinator = _FlipCoordinator(
        verification_engine=engine,
        evidence_orchestrator=orch_ev,
        observation_window_seconds=0.0,
    )

    orchestrator = ClosedLoopOrchestrator(
        investigation=_make_investigation(),
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=_make_deployment_pipeline(),
        verification=coordinator,
        repository=repo,
        remediation_planner=_counting_planner,
        max_reinvestigation_cycles=3,
    )

    result = await orchestrator.run(inc)

    assert result.resolved
    # Remediation called once per cycle
    assert len(planner_calls) >= 2


# ── 8. Maximum reinvestigation limit ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_closed_loop_escalates_when_max_cycles_reached() -> None:
    """ClosedLoopOrchestrator escalates when max_reinvestigation_cycles exceeded."""
    emitter = _EventCollector()
    repo = InMemoryIncidentRepository()
    inc = _incident()

    class _AlwaysFail(VerificationCoordinator):
        async def verify(self, incident: Incident, plan: Any, dep: Any, **kw: Any) -> Any:  # type: ignore[override]
            from backend.services.verification_coordinator import VerificationOutcome
            return VerificationOutcome(
                state=VerificationState.FAILED,
                incident_id=incident.incident_id,
                deployment_id=None,
                verification_result=None,
                observation_duration_ms=0.0,
                total_duration_ms=0.0,
                reinvestigate=True,
                failure_reason="Always fails",
            )

    snapshot = FakeMetricsSnapshot()
    engine = VerificationEngine(metrics_snapshot=snapshot)
    orch_ev = EvidenceOrchestrator(providers=[FakeMetricsProvider()])
    coordinator = _AlwaysFail(
        verification_engine=engine,
        evidence_orchestrator=orch_ev,
        event_emitter=emitter,
        observation_window_seconds=0.0,
    )

    orchestrator = ClosedLoopOrchestrator(
        investigation=_make_investigation(emitter=emitter),
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=_make_deployment_pipeline(emitter=emitter),
        verification=coordinator,
        repository=repo,
        remediation_planner=_noop_planner,
        event_emitter=emitter,
        max_reinvestigation_cycles=2,
    )

    result = await orchestrator.run(inc)

    assert result.status == "escalated"
    assert result.cycles == 2
    assert REINVESTIGATION_LIMIT_REACHED in emitter.names()
    assert INCIDENT_ESCALATED in emitter.names()


# ── 9. Deployment failure stops loop ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_closed_loop_stops_on_deployment_failure() -> None:
    """Closed loop returns failed status when deployment fails."""
    repo = InMemoryIncidentRepository()
    inc = _incident()

    pipeline = _make_deployment_pipeline(final_state=DeploymentState.FAILED)
    coordinator = _make_verification_coordinator()

    orchestrator = ClosedLoopOrchestrator(
        investigation=_make_investigation(),
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=pipeline,
        verification=coordinator,
        repository=repo,
        remediation_planner=_noop_planner,
        max_reinvestigation_cycles=3,
    )

    result = await orchestrator.run(inc)

    assert result.status == "failed"
    assert "deployment failed" in (result.failure_reason or "").lower()


# ── 10. Verification timeout ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verification_coordinator_times_out() -> None:
    """VerificationCoordinator returns TIMED_OUT when budget is exceeded."""
    emitter = _EventCollector()
    snapshot = FakeMetricsSnapshot(values={"svc-a": {"error_rate": 0.01}})
    engine = VerificationEngine(metrics_snapshot=snapshot)
    orch_ev = EvidenceOrchestrator(providers=[FakeMetricsProvider()])

    coordinator = VerificationCoordinator(
        verification_engine=engine,
        evidence_orchestrator=orch_ev,
        event_emitter=emitter,
        observation_window_seconds=1000.0,  # impossibly long
        verification_timeout_seconds=0.05,  # tiny budget
    )
    inc = _incident()
    plan = _make_verification_plan(inc.incident_id)

    outcome = await coordinator.verify(inc, plan, _deploy_result(incident_id=inc.incident_id))

    assert outcome.state == VerificationState.TIMED_OUT
    assert outcome.reinvestigate is True
    assert VERIFICATION_TIMED_OUT in emitter.names()


# ── 11. Verification cancellation ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verification_coordinator_cancellation() -> None:
    """Coordinator respects cancellation during observation window."""
    snapshot = FakeMetricsSnapshot()
    engine = VerificationEngine(metrics_snapshot=snapshot)
    orch_ev = EvidenceOrchestrator(providers=[FakeMetricsProvider()])

    coordinator = VerificationCoordinator(
        verification_engine=engine,
        evidence_orchestrator=orch_ev,
        observation_window_seconds=1000.0,  # long window
        verification_timeout_seconds=60.0,
    )
    inc = _incident()
    plan = _make_verification_plan(inc.incident_id)

    call_count = [0]

    def _cancel() -> bool:
        call_count[0] += 1
        return call_count[0] > 1

    outcome = await coordinator.verify(
        inc, plan, _deploy_result(incident_id=inc.incident_id),
        cancellation_check=_cancel,
    )

    assert outcome.state == VerificationState.CANCELLED


# ── 12. Evidence-provider partial failure ─────────────────────────────────────


@pytest.mark.asyncio
async def test_verification_continues_on_partial_evidence_failure() -> None:
    """Verification continues even when one evidence provider fails."""

    class _BrokenProvider:
        from backend.models.evidence import EvidenceSourceKind
        kind = EvidenceSourceKind.LOGS
        name = "broken"

        async def collect(self, inc: Any, *, max_items: int = 20, context: Any = None) -> list[Any]:
            raise RuntimeError("provider down")

        async def health_check(self) -> bool:
            return False

    snapshot = FakeMetricsSnapshot(values={"svc-a": {"error_rate": 0.01}})
    engine = VerificationEngine(metrics_snapshot=snapshot)
    orch_ev = EvidenceOrchestrator(
        providers=[FakeMetricsProvider(), _BrokenProvider()]
    )
    coordinator = VerificationCoordinator(
        verification_engine=engine,
        evidence_orchestrator=orch_ev,
        observation_window_seconds=0.0,
    )
    inc = _incident()
    plan = _make_verification_plan(inc.incident_id)

    outcome = await coordinator.verify(inc, plan, _deploy_result(incident_id=inc.incident_id))

    # Should still complete — partial evidence is tolerated
    assert outcome.state in (VerificationState.PASSED, VerificationState.FAILED)


# ── 13. Duplicate verification (already-resolved) ────────────────────────────


@pytest.mark.asyncio
async def test_closed_loop_skips_already_resolved_incident() -> None:
    """ClosedLoopOrchestrator skips the cycle if the incident is already RESOLVED."""
    repo = InMemoryIncidentRepository()
    inc = _incident(inc_id="inc-already-resolved")
    resolved_inc = inc.with_status(IncidentStatus.RESOLVED)
    await repo.save(resolved_inc)

    coordinator = _make_verification_coordinator()
    orchestrator = ClosedLoopOrchestrator(
        investigation=_make_investigation(),
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=_make_deployment_pipeline(),
        verification=coordinator,
        repository=repo,
        max_reinvestigation_cycles=3,
    )

    result = await orchestrator.run(inc)

    # Should exit immediately with resolved status
    assert result.resolved
    assert result.cycles == 0


# ── 14. Idempotent retry ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idempotent_retry_does_not_create_duplicate_resolution() -> None:
    """Running orchestrator twice on the same incident does not break idempotency."""
    repo = InMemoryIncidentRepository()
    inc = _incident(inc_id="inc-idem-001")
    emitter = _EventCollector()
    coordinator = _make_verification_coordinator(emitter=emitter)
    policy = RemediationPolicyEngine(
        policy=ActionPolicy(
            auto_approve_risk_levels=frozenset({
                ActionRiskLevel.LOW, ActionRiskLevel.MEDIUM, ActionRiskLevel.HIGH,
            })
        )
    )
    pipeline = DeploymentPipeline(
        provider=FakeDeploymentProvider(final_state=DeploymentState.SUCCEEDED),
        policy_engine=policy,
        event_emitter=emitter,
    )

    orchestrator = ClosedLoopOrchestrator(
        investigation=_make_investigation(emitter=emitter),
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=pipeline,
        verification=coordinator,
        repository=repo,
        remediation_planner=_noop_planner,
        event_emitter=emitter,
        max_reinvestigation_cycles=3,
    )

    result1 = await orchestrator.run(inc)
    result2 = await orchestrator.run(inc)

    # Second run should detect already-resolved and skip
    assert result1.resolved
    assert result2.resolved
    assert result2.cycles == 0  # skipped immediately


# ── 15. Resolution persistence ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_incident_status_persisted_to_repository_on_resolution() -> None:
    """Incident status is RESOLVED in the repository after closed-loop success."""
    repo = InMemoryIncidentRepository()
    inc = _incident(inc_id="inc-persist-001")
    coordinator = _make_verification_coordinator()
    policy = RemediationPolicyEngine(
        policy=ActionPolicy(
            auto_approve_risk_levels=frozenset({
                ActionRiskLevel.LOW, ActionRiskLevel.MEDIUM, ActionRiskLevel.HIGH,
            })
        )
    )
    pipeline = DeploymentPipeline(
        provider=FakeDeploymentProvider(final_state=DeploymentState.SUCCEEDED),
        policy_engine=policy,
    )

    orchestrator = ClosedLoopOrchestrator(
        investigation=_make_investigation(),
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=pipeline,
        verification=coordinator,
        repository=repo,
        remediation_planner=_noop_planner,
        max_reinvestigation_cycles=3,
    )

    result = await orchestrator.run(inc)

    assert result.resolved
    saved = await repo.get("inc-persist-001")
    assert saved is not None
    assert saved.status == IncidentStatus.RESOLVED


# ── 16. Audit event emission ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_pass_path_emits_all_expected_events() -> None:
    """Pass path emits VerificationStarted, Passed, Completed, IncidentResolved."""
    emitter = _EventCollector()
    repo = InMemoryIncidentRepository()
    inc = _incident()
    coordinator = _make_verification_coordinator(emitter=emitter)
    policy = RemediationPolicyEngine(
        policy=ActionPolicy(
            auto_approve_risk_levels=frozenset({
                ActionRiskLevel.LOW, ActionRiskLevel.MEDIUM, ActionRiskLevel.HIGH,
            })
        )
    )
    pipeline = DeploymentPipeline(
        provider=FakeDeploymentProvider(final_state=DeploymentState.SUCCEEDED),
        policy_engine=policy,
        event_emitter=emitter,
    )

    orchestrator = ClosedLoopOrchestrator(
        investigation=_make_investigation(emitter=emitter),
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=pipeline,
        verification=coordinator,
        repository=repo,
        remediation_planner=_noop_planner,
        event_emitter=emitter,
        max_reinvestigation_cycles=3,
    )

    await orchestrator.run(inc)

    names = emitter.names()
    assert VERIFICATION_STARTED in names
    assert VERIFICATION_PASSED in names
    assert VERIFICATION_COMPLETED in names
    assert INCIDENT_RESOLVED in names


# ── 17. OTel/correlation propagation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_events_carry_incident_id_throughout_loop() -> None:
    """All lifecycle events carry the correct incident_id."""
    emitter = _EventCollector()
    repo = InMemoryIncidentRepository()
    inc = _incident(inc_id="inc-corr-test")
    coordinator = _make_verification_coordinator(emitter=emitter)
    policy = RemediationPolicyEngine(
        policy=ActionPolicy(
            auto_approve_risk_levels=frozenset({
                ActionRiskLevel.LOW, ActionRiskLevel.MEDIUM, ActionRiskLevel.HIGH,
            })
        )
    )
    pipeline = DeploymentPipeline(
        provider=FakeDeploymentProvider(final_state=DeploymentState.SUCCEEDED),
        policy_engine=policy,
        event_emitter=emitter,
    )

    orchestrator = ClosedLoopOrchestrator(
        investigation=_make_investigation(emitter=emitter),
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=pipeline,
        verification=coordinator,
        repository=repo,
        remediation_planner=_noop_planner,
        event_emitter=emitter,
        max_reinvestigation_cycles=2,
    )

    await orchestrator.run(inc)

    for name, payload in emitter.events:
        if "incident_id" in payload:
            assert payload["incident_id"] == "inc-corr-test", (
                f"Event {name!r} has wrong incident_id"
            )


# ── 18. Complete mocked closed-loop: pass path ───────────────────────────────


@pytest.mark.asyncio
async def test_complete_closed_loop_pass_path() -> None:
    """End-to-end: investigation → remediation → deployment → verification → resolved."""
    emitter = _EventCollector()
    repo = InMemoryIncidentRepository()
    inc = _incident(inc_id="inc-full-pass")

    coordinator = _make_verification_coordinator(
        after_values={"error_rate": 0.01},
        emitter=emitter,
    )
    policy = RemediationPolicyEngine(
        policy=ActionPolicy(
            auto_approve_risk_levels=frozenset({
                ActionRiskLevel.LOW, ActionRiskLevel.MEDIUM, ActionRiskLevel.HIGH,
            })
        )
    )
    pipeline = DeploymentPipeline(
        provider=FakeDeploymentProvider(final_state=DeploymentState.SUCCEEDED),
        policy_engine=policy,
        event_emitter=emitter,
    )

    orchestrator = ClosedLoopOrchestrator(
        investigation=_make_investigation(emitter=emitter),
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=pipeline,
        verification=coordinator,
        repository=repo,
        remediation_planner=_noop_planner,
        event_emitter=emitter,
        max_reinvestigation_cycles=3,
    )

    result = await orchestrator.run(inc, execution_id="exec-full-pass")

    assert result.resolved
    assert result.cycles == 1
    assert result.resolution is not None
    assert result.resolution.status == ResolutionStatus.RESOLVED
    saved = await repo.get("inc-full-pass")
    assert saved is not None and saved.status == IncidentStatus.RESOLVED


# ── 19. Closed-loop failure → reinvestigation → resolved ─────────────────────


@pytest.mark.asyncio
async def test_complete_closed_loop_fail_then_resolve() -> None:
    """End-to-end: first verification fails, second succeeds → resolved."""
    emitter = _EventCollector()
    repo = InMemoryIncidentRepository()
    inc = _incident(inc_id="inc-fail-then-pass")
    cycle_count = [0]

    class _FlipCoordinator(VerificationCoordinator):
        async def verify(self, incident: Incident, plan: Any, dep: Any, **kw: Any) -> Any:  # type: ignore[override]
            from backend.services.verification_coordinator import VerificationOutcome
            cycle_count[0] += 1
            passed = cycle_count[0] >= 2
            await self._emit(
                VERIFICATION_PASSED if passed else VERIFICATION_FAILED,
                {"incident_id": incident.incident_id, "cycle": cycle_count[0]},
            )
            return VerificationOutcome(
                state=VerificationState.PASSED if passed else VerificationState.FAILED,
                incident_id=incident.incident_id,
                deployment_id=dep.request.deployment_id if dep else None,
                verification_result=None,
                observation_duration_ms=0.0,
                total_duration_ms=0.0,
                reinvestigate=not passed,
                failure_reason=None if passed else "First cycle degraded",
            )

    snapshot = FakeMetricsSnapshot()
    engine = VerificationEngine(metrics_snapshot=snapshot)
    orch_ev = EvidenceOrchestrator(providers=[FakeMetricsProvider()])
    coordinator = _FlipCoordinator(
        verification_engine=engine,
        evidence_orchestrator=orch_ev,
        event_emitter=emitter,
        observation_window_seconds=0.0,
    )
    policy = RemediationPolicyEngine(
        policy=ActionPolicy(
            auto_approve_risk_levels=frozenset({
                ActionRiskLevel.LOW, ActionRiskLevel.MEDIUM, ActionRiskLevel.HIGH,
            })
        )
    )
    pipeline = DeploymentPipeline(
        provider=FakeDeploymentProvider(final_state=DeploymentState.SUCCEEDED),
        policy_engine=policy,
        event_emitter=emitter,
    )

    orchestrator = ClosedLoopOrchestrator(
        investigation=_make_investigation(emitter=emitter),
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=pipeline,
        verification=coordinator,
        repository=repo,
        remediation_planner=_noop_planner,
        event_emitter=emitter,
        max_reinvestigation_cycles=3,
    )

    result = await orchestrator.run(inc)

    assert result.resolved
    assert result.cycles == 2
    assert REINVESTIGATION_STARTED in emitter.names()
    assert INCIDENT_RESOLVED in emitter.names()


# ── 20. Closed-loop max-cycles escalation ─────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_closed_loop_max_cycles_escalation() -> None:
    """End-to-end: all verification cycles fail → escalated."""
    emitter = _EventCollector()
    repo = InMemoryIncidentRepository()
    inc = _incident(inc_id="inc-max-cycles")

    class _AlwaysFail(VerificationCoordinator):
        async def verify(self, incident: Incident, plan: Any, dep: Any, **kw: Any) -> Any:  # type: ignore[override]
            from backend.services.verification_coordinator import VerificationOutcome
            return VerificationOutcome(
                state=VerificationState.FAILED,
                incident_id=incident.incident_id,
                deployment_id=None,
                verification_result=None,
                observation_duration_ms=0.0,
                total_duration_ms=0.0,
                reinvestigate=True,
                failure_reason="Always failing",
            )

    snapshot = FakeMetricsSnapshot()
    engine = VerificationEngine(metrics_snapshot=snapshot)
    orch_ev = EvidenceOrchestrator(providers=[FakeMetricsProvider()])
    coordinator = _AlwaysFail(
        verification_engine=engine,
        evidence_orchestrator=orch_ev,
        event_emitter=emitter,
        observation_window_seconds=0.0,
    )
    policy = RemediationPolicyEngine(
        policy=ActionPolicy(
            auto_approve_risk_levels=frozenset({
                ActionRiskLevel.LOW, ActionRiskLevel.MEDIUM, ActionRiskLevel.HIGH,
            })
        )
    )
    pipeline = DeploymentPipeline(
        provider=FakeDeploymentProvider(final_state=DeploymentState.SUCCEEDED),
        policy_engine=policy,
        event_emitter=emitter,
    )

    orchestrator = ClosedLoopOrchestrator(
        investigation=_make_investigation(emitter=emitter),
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=pipeline,
        verification=coordinator,
        repository=repo,
        remediation_planner=_noop_planner,
        event_emitter=emitter,
        max_reinvestigation_cycles=2,
    )

    result = await orchestrator.run(inc)

    assert result.status == "escalated"
    assert result.cycles == 2
    assert REINVESTIGATION_LIMIT_REACHED in emitter.names()
    assert INCIDENT_ESCALATED in emitter.names()
    # Default verification plan function works for any incident
    plan = _default_verification_plan(inc)
    assert plan.incident_id == inc.incident_id
    assert "error_rate" in plan.metrics_to_compare


# ── 21. Unexpected exception from investigation is caught and returned as failed ───────


@pytest.mark.asyncio
async def test_closed_loop_catches_investigation_exception() -> None:
    """Verify run() returns ClosedLoopResult(status=failed) when investigation raises."""
    emitter = _EventCollector()
    repo = InMemoryIncidentRepository()
    inc = _incident(inc_id="exc-investigation")

    class FailingInvestigation:
        """Mock investigation that raises an unexpected exception."""
        async def investigate(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Simulated investigation failure")

    coordinator = _make_verification_coordinator(emitter=emitter)
    pipeline = _make_deployment_pipeline()

    orchestrator = ClosedLoopOrchestrator(
        investigation=FailingInvestigation(),  # type: ignore[arg-type]
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=pipeline,
        verification=coordinator,
        repository=repo,
        remediation_planner=_noop_planner,
        event_emitter=emitter,
        max_reinvestigation_cycles=3,
    )

    # Must NOT raise; must return a ClosedLoopResult
    result = await orchestrator.run(inc, execution_id="exec-exc-test")

    # Verify result structure
    assert result is not None
    assert result.status == "failed"
    assert result.incident_id == inc.incident_id
    assert result.cycles == 0
    assert result.duration_ms > 0.0

    # Verify failure_reason contains exception details
    assert result.failure_reason is not None
    assert "RuntimeError" in result.failure_reason
    assert "investigation failure" in result.failure_reason

    # Verify incident was persisted with ESCALATED status (cannot auto-resolve)
    saved = await repo.get(inc.incident_id)
    assert saved is not None
    assert saved.status == IncidentStatus.ESCALATED


# ── 22. Unexpected exception from verification is caught and returned as failed ───────


@pytest.mark.asyncio
async def test_closed_loop_catches_verification_exception() -> None:
    """Verify run() returns ClosedLoopResult(status=failed) when verification raises."""
    emitter = _EventCollector()
    repo = InMemoryIncidentRepository()
    inc = _incident(inc_id="exc-verification")

    class FailingVerification:
        """Mock verification coordinator that raises an unexpected exception."""
        async def verify(self, *args: Any, **kwargs: Any) -> Any:
            raise ValueError("Verification engine internal error")

    coordinator = FailingVerification()
    pipeline = _make_deployment_pipeline()

    orchestrator = ClosedLoopOrchestrator(
        investigation=_make_investigation(emitter=emitter),
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=pipeline,
        verification=coordinator,  # type: ignore[arg-type]
        repository=repo,
        remediation_planner=_noop_planner,
        event_emitter=emitter,
        max_reinvestigation_cycles=3,
    )

    result = await orchestrator.run(inc, execution_id="exec-exc-verify")

    # Verify failure result
    assert result.status == "failed"
    assert result.failure_reason is not None
    assert "ValueError" in result.failure_reason
    assert "Verification engine internal error" in result.failure_reason

    # Verify incident was persisted with ESCALATED status
    saved = await repo.get(inc.incident_id)
    assert saved is not None
    assert saved.status == IncidentStatus.ESCALATED


# ── 23. Unexpected exception from deployment pipeline is caught and returned as failed ──


@pytest.mark.asyncio
async def test_closed_loop_catches_deployment_exception() -> None:
    """Verify run() catches exceptions that escape deployment_pipeline.deploy()."""
    emitter = _EventCollector()
    repo = InMemoryIncidentRepository()
    inc = _incident(inc_id="exc-deployment")

    class FailingDeploymentPipeline:
        """Mock deployment pipeline that raises an unexpected exception."""
        async def deploy(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Deployment system failure")

    coordinator = _make_verification_coordinator(emitter=emitter)
    pipeline = FailingDeploymentPipeline()

    orchestrator = ClosedLoopOrchestrator(
        investigation=_make_investigation(emitter=emitter),
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=pipeline,  # type: ignore[arg-type]
        verification=coordinator,
        repository=repo,
        remediation_planner=_noop_planner,
        event_emitter=emitter,
        max_reinvestigation_cycles=3,
    )

    result = await orchestrator.run(inc, execution_id="exec-exc-deploy")

    assert result.status == "failed"
    assert result.failure_reason is not None
    assert "RuntimeError" in result.failure_reason
    assert "Deployment system failure" in result.failure_reason

    # Verify incident was persisted with ESCALATED status
    saved = await repo.get(inc.incident_id)
    assert saved is not None
    assert saved.status == IncidentStatus.ESCALATED


# ── 24. Repository.save() failure during FAILED status persistence doesn't raise ──────


@pytest.mark.asyncio
async def test_closed_loop_survives_failed_status_persistence() -> None:
    """Verify run() returns failed result even if persisting FAILED status fails."""
    emitter = _EventCollector()
    
    class FailingRepository:
        """Mock repository that fails to save."""
        async def get(self, incident_id: str) -> Any:
            return None
        
        async def save(self, incident: Any) -> None:
            raise OSError("Database connection lost")

    repo = FailingRepository()
    inc = _incident(inc_id="exc-repo-fail")

    class FailingInvestigation:
        """Investigation that raises to trigger the exception handler."""
        async def investigate(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Investigation failed")

    coordinator = _make_verification_coordinator(emitter=emitter)
    pipeline = _make_deployment_pipeline()

    orchestrator = ClosedLoopOrchestrator(
        investigation=FailingInvestigation(),  # type: ignore[arg-type]
        remediation_engine=_make_remediation_engine(),
        deployment_pipeline=pipeline,
        verification=coordinator,
        repository=repo,  # type: ignore[arg-type]
        remediation_planner=_noop_planner,
        event_emitter=emitter,
        max_reinvestigation_cycles=3,
    )

    # Must NOT raise even though repository.save() fails
    result = await orchestrator.run(inc, execution_id="exec-exc-repo")

    # Should still return failed result
    assert result is not None
    assert result.status == "failed"
    assert result.failure_reason is not None
    assert "RuntimeError" in result.failure_reason
    # The result should reflect the original investigation failure, not the repo failure
    assert "Investigation failed" in result.failure_reason


# ── 25. Run() never raises; always returns ClosedLoopResult ────────────────────────


@pytest.mark.asyncio
async def test_closed_loop_run_never_raises() -> None:
    """Verify run() honors its contract: never raises, always returns ClosedLoopResult."""
    emitter = _EventCollector()
    repo = InMemoryIncidentRepository()

    # Multiple failure scenarios
    failure_scenarios = [
        ("investigation_error", RuntimeError("Investigation error")),
        ("verification_error", ValueError("Verification error")),
        ("unknown_error", Exception("Unknown error")),
    ]

    for scenario_id, exception_to_raise in failure_scenarios:
        inc = _incident(inc_id=f"never-raise-{scenario_id}")

        class FailingInvestigation:
            def __init__(self, exc: Exception) -> None:
                self.exc = exc

            async def investigate(self, *args: Any, **kwargs: Any) -> Any:
                raise self.exc

        coordinator = _make_verification_coordinator(emitter=emitter)
        pipeline = _make_deployment_pipeline()

        orchestrator = ClosedLoopOrchestrator(
            investigation=FailingInvestigation(exception_to_raise),  # type: ignore[arg-type]
            remediation_engine=_make_remediation_engine(),
            deployment_pipeline=pipeline,
            verification=coordinator,
            repository=repo,
            remediation_planner=_noop_planner,
            event_emitter=emitter,
            max_reinvestigation_cycles=3,
        )

        # This must not raise
        result = await orchestrator.run(inc)

        # Result must always be present and be a ClosedLoopResult
        assert result is not None
        assert isinstance(result, ClosedLoopResult)
        assert result.status == "failed"
        assert result.incident_id == inc.incident_id
