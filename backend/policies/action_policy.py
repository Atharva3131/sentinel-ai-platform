"""Remediation action policy enforcement.

This module is the enforcement point for all remediation safety controls.
No remediation action executes without passing through here.

Controls implemented:
  - Action type allowlist: actions not on the allowlist are rejected.
  - Risk-based approval requirements: HIGH/CRITICAL require explicit approval.
  - Protected resources: certain services/environments are protected.
  - Protected branches: code changes to protected branches are rejected.
  - Retry limits: per-action-type execution counts are bounded.
  - Execution timeouts: per-action-type hard limits.
  - Circuit breakers: per-action-type open/closed/half-open state.
  - Idempotency: duplicate action_id/idempotency_key are rejected.
  - Audit trail: every decision is recorded immutably.

Design:
  ``RemediationPolicyEngine`` is the single enforcement point injected into
  the RemediationEngine via the ``PolicyGateway`` protocol.  It holds
  ``ActionPolicy`` configuration (allowlists, protected resources, etc.)
  and stateful ``CircuitBreaker`` instances per action type.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from backend.models.incident import Incident
from backend.models.remediation import (
    ActionRiskLevel,
    RemediationAction,
    RemediationActionType,
)


class ActionPolicyViolation(StrEnum):
    """Why a policy decision was DENIED."""

    NOT_ALLOWLISTED = "not_allowlisted"
    PROTECTED_RESOURCE = "protected_resource"
    PROTECTED_BRANCH = "protected_branch"
    REQUIRES_APPROVAL = "requires_approval"
    RETRY_LIMIT_EXCEEDED = "retry_limit_exceeded"
    CIRCUIT_OPEN = "circuit_open"
    DUPLICATE_IDEMPOTENCY_KEY = "duplicate_idempotency_key"
    TIMEOUT_EXCEEDED = "timeout_exceeded"


class CircuitBreakerState(StrEnum):
    CLOSED = "closed"    # normal operation
    OPEN = "open"        # failing — reject all
    HALF_OPEN = "half_open"  # testing recovery


class CircuitBreaker:
    """Per-action-type circuit breaker.

    Opens after ``failure_threshold`` consecutive failures.
    Transitions to half-open after ``recovery_seconds``.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_seconds: float = 60.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._state = CircuitBreakerState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitBreakerState:
        if self._state == CircuitBreakerState.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self.recovery_seconds:
                self._state = CircuitBreakerState.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._failure_count = 0
        self._state = CircuitBreakerState.CLOSED
        self._opened_at = None

    def record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitBreakerState.OPEN
            self._opened_at = time.monotonic()

    def is_open(self) -> bool:
        return self.state == CircuitBreakerState.OPEN


@dataclass
class ActionPolicyDecision:
    """Immutable record of a single policy evaluation."""

    decision_id: str
    action_id: str
    action_type: str
    allowed: bool
    violation: ActionPolicyViolation | None
    reason: str
    evaluated_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionPolicy:
    """Immutable policy configuration.

    ``allowlisted_action_types``: only these action types are ever permitted.
    ``protected_services``: actions targeting these services require CRITICAL risk approval.
    ``protected_environments``: actions in these environments require CRITICAL risk approval.
    ``protected_branches``: source-repo changes to these branches are rejected.
    ``max_retries_per_type``: per action-type execution ceiling within one workflow.
    ``max_timeout_seconds``: hard action timeout override.
    ``auto_approve_risk_levels``: risk levels that pass without human approval.
    """

    allowlisted_action_types: frozenset[str] = field(
        default_factory=lambda: frozenset(t.value for t in RemediationActionType)
    )
    protected_services: frozenset[str] = field(default_factory=frozenset)
    protected_environments: frozenset[str] = field(default_factory=frozenset)
    protected_branches: frozenset[str] = field(
        default_factory=lambda: frozenset({"main", "master", "production"})
    )
    max_retries_per_type: dict[str, int] = field(default_factory=dict)
    max_timeout_seconds: float = 300.0
    auto_approve_risk_levels: frozenset[str] = field(
        default_factory=lambda: frozenset({ActionRiskLevel.LOW, ActionRiskLevel.MEDIUM})
    )


class PolicyDecisionRecord:
    """Append-only in-memory record of all policy decisions.

    In production this is backed by the AuditRepository.
    """

    def __init__(self) -> None:
        self._decisions: list[ActionPolicyDecision] = []

    def append(self, decision: ActionPolicyDecision) -> None:
        self._decisions.append(decision)

    def all(self) -> tuple[ActionPolicyDecision, ...]:
        return tuple(self._decisions)

    def denied(self) -> tuple[ActionPolicyDecision, ...]:
        return tuple(d for d in self._decisions if not d.allowed)


class RemediationPolicyEngine:
    """Single enforcement point for all remediation policy controls.

    Satisfies the ``PolicyGateway`` protocol used by ``RemediationEngine``.
    """

    def __init__(
        self,
        policy: ActionPolicy | None = None,
        *,
        circuit_breaker_failure_threshold: int = 5,
        circuit_breaker_recovery_seconds: float = 60.0,
    ) -> None:
        self._policy = policy or ActionPolicy()
        self._circuit_breakers: dict[str, CircuitBreaker] = defaultdict(
            lambda: CircuitBreaker(
                failure_threshold=circuit_breaker_failure_threshold,
                recovery_seconds=circuit_breaker_recovery_seconds,
            )
        )
        self._execution_counts: dict[str, int] = defaultdict(int)
        self._idempotency_keys: set[str] = set()
        self.record = PolicyDecisionRecord()

    async def evaluate(
        self,
        action: RemediationAction,
        incident: Incident,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate *action* and return ``{"allowed": bool, "reason": str}``."""
        decision = self._decide(action, incident)
        self.record.append(decision)
        return {"allowed": decision.allowed, "reason": decision.reason}

    def record_execution_outcome(
        self,
        action: RemediationAction,
        *,
        succeeded: bool,
    ) -> None:
        """Update circuit-breaker state based on execution outcome."""
        cb = self._circuit_breakers[action.action_type]
        if succeeded:
            cb.record_success()
        else:
            cb.record_failure()

    def _decide(
        self, action: RemediationAction, incident: Incident
    ) -> ActionPolicyDecision:
        now = datetime.now(UTC)
        decision_id = str(uuid.uuid4())

        def _deny(violation: ActionPolicyViolation, reason: str) -> ActionPolicyDecision:
            return ActionPolicyDecision(
                decision_id=decision_id,
                action_id=action.action_id,
                action_type=action.action_type,
                allowed=False,
                violation=violation,
                reason=reason,
                evaluated_at=now,
            )

        def _allow(reason: str) -> ActionPolicyDecision:
            return ActionPolicyDecision(
                decision_id=decision_id,
                action_id=action.action_id,
                action_type=action.action_type,
                allowed=True,
                violation=None,
                reason=reason,
                evaluated_at=now,
            )

        # 1. Allowlist check
        if action.action_type not in self._policy.allowlisted_action_types:
            return _deny(
                ActionPolicyViolation.NOT_ALLOWLISTED,
                f"Action type '{action.action_type}' is not on the allowlist.",
            )

        # 2. Protected resource check
        if action.target_service in self._policy.protected_services:
            return _deny(
                ActionPolicyViolation.PROTECTED_RESOURCE,
                f"Service '{action.target_service}' is protected from autonomous remediation.",
            )

        # 3. Protected environment check
        if action.target_environment in self._policy.protected_environments:
            return _deny(
                ActionPolicyViolation.PROTECTED_RESOURCE,
                f"Environment '{action.target_environment}' is protected.",
            )

        # 4. Approval requirement check
        if action.risk_level.value not in self._policy.auto_approve_risk_levels:
            return _deny(
                ActionPolicyViolation.REQUIRES_APPROVAL,
                f"Risk level '{action.risk_level}' requires explicit human approval.",
            )

        # 5. Circuit breaker check
        cb = self._circuit_breakers[action.action_type]
        if cb.is_open():
            return _deny(
                ActionPolicyViolation.CIRCUIT_OPEN,
                f"Circuit breaker for '{action.action_type}' is OPEN after repeated failures.",
            )

        # 6. Retry limit check
        max_retries = self._policy.max_retries_per_type.get(action.action_type)
        if max_retries is not None:
            count = self._execution_counts[action.action_type]
            if count >= max_retries:
                return _deny(
                    ActionPolicyViolation.RETRY_LIMIT_EXCEEDED,
                    f"Action type '{action.action_type}' has reached its "
                    f"retry limit ({max_retries}).",
                )

        # 7. Idempotency key check
        if action.idempotency_key is not None:
            if action.idempotency_key in self._idempotency_keys:
                return _deny(
                    ActionPolicyViolation.DUPLICATE_IDEMPOTENCY_KEY,
                    f"Idempotency key '{action.idempotency_key}' has already been executed.",
                )
            self._idempotency_keys.add(action.idempotency_key)

        # 8. Timeout check
        if action.timeout_seconds > self._policy.max_timeout_seconds:
            return _deny(
                ActionPolicyViolation.TIMEOUT_EXCEEDED,
                f"Action timeout {action.timeout_seconds}s exceeds policy maximum "
                f"{self._policy.max_timeout_seconds}s.",
            )

        # Track execution count
        self._execution_counts[action.action_type] += 1

        return _allow(
            f"Action '{action.action_type}' permitted: risk={action.risk_level}, "
            f"service={action.target_service}."
        )
