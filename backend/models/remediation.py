"""Remediation domain models — plan, actions, results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class RemediationActionType(StrEnum):
    """Generic remediation action verb.

    These are the allowlisted operation types.  New types must be explicitly
    added here and approved in the policy layer before the executor can run them.
    """

    # Service operations
    RESTART_SERVICE = "restart_service"
    SCALE_SERVICE = "scale_service"
    DRAIN_TRAFFIC = "drain_traffic"
    ENABLE_FEATURE_FLAG = "enable_feature_flag"
    DISABLE_FEATURE_FLAG = "disable_feature_flag"

    # Deployment operations
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    REDEPLOY = "redeploy"
    PROMOTE_CANARY = "promote_canary"
    PAUSE_ROLLOUT = "pause_rollout"

    # Cache / data operations
    CLEAR_CACHE = "clear_cache"
    WARM_CACHE = "warm_cache"
    INVALIDATE_CACHE_KEY = "invalidate_cache_key"

    # Configuration operations
    UPDATE_CONFIG = "update_config"
    REVERT_CONFIG = "revert_config"

    # Infrastructure operations
    OPEN_CIRCUIT_BREAKER = "open_circuit_breaker"
    CLOSE_CIRCUIT_BREAKER = "close_circuit_breaker"
    INCREASE_RESOURCE_QUOTA = "increase_resource_quota"
    ROTATE_CREDENTIALS = "rotate_credentials"

    # Custom / extension point
    CUSTOM = "custom"


class ActionRiskLevel(StrEnum):
    """Risk classification determining approval requirements."""

    LOW = "low"          # no approval required; reversible; read-like side effects
    MEDIUM = "medium"    # automatic approval with policy gate
    HIGH = "high"        # explicit human approval required
    CRITICAL = "critical"  # human approval + change-window enforcement


class ActionStatus(StrEnum):
    """Execution lifecycle state of a single remediation action."""

    PENDING = "pending"
    POLICY_PENDING = "policy_pending"   # awaiting policy evaluation
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class RemediationAction:
    """A single atomic remediation step.

    Every action exposes the full contract required by the policy layer,
    executor, and audit trail.  No field is optional except those that
    are genuinely unknown at planning time.

    ``rollback_action_type`` names the inverse operation (e.g. ROLLBACK_DEPLOYMENT
    for a REDEPLOY action).  When None, rollback is not supported.
    ``idempotency_key`` allows the executor to safely retry without double-applying.
    """

    action_id: str
    action_type: RemediationActionType
    target_service: str
    target_environment: str
    risk_level: ActionRiskLevel
    title: str
    description: str
    parameters: dict[str, Any]
    # Governance
    permissions_required: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    expected_effect: str = ""
    rollback_action_type: RemediationActionType | None = None
    rollback_parameters: dict[str, Any] = field(default_factory=dict)
    # Execution controls
    timeout_seconds: float = 60.0
    idempotency_key: str | None = None
    requires_validation: bool = True
    status: ActionStatus = ActionStatus.PENDING
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_reversible(self) -> bool:
        return self.rollback_action_type is not None

    @property
    def requires_approval(self) -> bool:
        return self.risk_level in (ActionRiskLevel.HIGH, ActionRiskLevel.CRITICAL)


@dataclass(frozen=True, slots=True)
class RemediationPlan:
    """Ordered set of remediation actions for one incident.

    ``actions`` is ordered by execution sequence.  Actions in the same
    ``stage`` index may execute concurrently; stages execute sequentially.
    ``policy_evaluated`` flags whether the policy layer has assessed each action.
    """

    plan_id: str
    incident_id: str
    rca_id: str | None
    actions: tuple[RemediationAction, ...]
    stages: tuple[tuple[str, ...], ...]   # tuples of action_id per stage
    created_at: datetime
    policy_evaluated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def action_count(self) -> int:
        return len(self.actions)

    def by_id(self, action_id: str) -> RemediationAction | None:
        return next((a for a in self.actions if a.action_id == action_id), None)

    def pending(self) -> tuple[RemediationAction, ...]:
        return tuple(a for a in self.actions if a.status == ActionStatus.PENDING)

    def approved(self) -> tuple[RemediationAction, ...]:
        return tuple(a for a in self.actions if a.status == ActionStatus.APPROVED)

    def high_risk(self) -> tuple[RemediationAction, ...]:
        return tuple(
            a for a in self.actions
            if a.risk_level in (ActionRiskLevel.HIGH, ActionRiskLevel.CRITICAL)
        )


@dataclass(frozen=True, slots=True)
class RemediationResult:
    """Aggregated outcome of executing a remediation plan.

    ``action_results`` maps action_id → outcome dict with keys:
    ``status``, ``output``, ``error``, ``duration_ms``, ``rolled_back``.
    """

    plan_id: str
    incident_id: str
    succeeded: bool
    actions_attempted: int
    actions_succeeded: int
    actions_failed: int
    actions_rolled_back: int
    duration_ms: float
    action_results: dict[str, Any] = field(default_factory=dict)
    failure_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
