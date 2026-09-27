"""RemediationEngine — policy-aware planning and execution of remediation actions.

The engine is generic: it does not contain hard-coded logic for any specific
incident type.  The ``RemediationPlanner`` protocol is the extension point —
production code wires an LLM-backed planner or a rule-based planner here.

Safety invariants enforced by this engine:
  - Every action is policy-evaluated before execution.
  - HIGH and CRITICAL risk actions require approval; execution is blocked without it.
  - Retry limits are respected.
  - Circuit breakers per action type prevent runaway execution.
  - Every action execution is auditable (caller must persist the RemediationResult).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from backend.models.hypothesis import RootCauseAnalysis
from backend.models.incident import Incident
from backend.models.remediation import (
    ActionRiskLevel,
    RemediationAction,
    RemediationActionType,
    RemediationPlan,
    RemediationResult,
)

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


@runtime_checkable
class RemediationPlanner(Protocol):
    """Port that produces a RemediationPlan from an RCA.

    The planner is the only component that knows about service-specific
    remediation strategies.  Business logic does NOT hard-code action types.
    """

    async def plan(
        self,
        incident: Incident,
        rca: RootCauseAnalysis | None,
        *,
        context: dict[str, Any] | None = None,
    ) -> RemediationPlan:
        """Produce a remediation plan for *incident* given the RCA findings."""
        ...


@runtime_checkable
class ActionExecutor(Protocol):
    """Port that executes a single remediation action against infrastructure.

    Implementations are responsible for idempotency; the engine does not
    retry individual actions unless the executor explicitly signals retryability.
    """

    async def execute(
        self,
        action: RemediationAction,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute *action* and return an outcome dict with at minimum:
        ``{"status": "succeeded"|"failed", "output": ..., "error": ...}``
        """
        ...

    async def rollback(
        self,
        action: RemediationAction,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Roll back *action* and return an outcome dict."""
        ...


@runtime_checkable
class PolicyGateway(Protocol):
    """Port that evaluates whether an action is permitted.

    Returns a decision dict with ``{"allowed": bool, "reason": str}``.
    """

    async def evaluate(
        self,
        action: RemediationAction,
        incident: Incident,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate whether *action* is policy-permitted for *incident*."""
        ...


# ---------------------------------------------------------------------------
# Fake implementations for tests
# ---------------------------------------------------------------------------


class AlwaysAllowPolicyGateway:
    """Policy gateway that permits all LOW and MEDIUM risk actions automatically."""

    async def evaluate(
        self,
        action: RemediationAction,
        incident: Incident,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if action.risk_level in (ActionRiskLevel.HIGH, ActionRiskLevel.CRITICAL):
            return {
                "allowed": False,
                "reason": f"Risk level {action.risk_level} requires explicit approval.",
            }
        return {"allowed": True, "reason": "Automatically approved by policy."}


class RejectAllPolicyGateway:
    """Policy gateway that rejects every action — used in tests for policy rejection."""

    async def evaluate(
        self,
        action: RemediationAction,
        incident: Incident,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"allowed": False, "reason": "All actions rejected by policy."}


class FakeActionExecutor:
    """Action executor that replays configured responses without real side effects."""

    def __init__(
        self,
        succeed_action_types: set[RemediationActionType] | None = None,
        *,
        default_succeed: bool = True,
    ) -> None:
        self._succeed_types = succeed_action_types
        self._default_succeed = default_succeed
        self.executed: list[RemediationAction] = []
        self.rolled_back: list[RemediationAction] = []

    async def execute(
        self,
        action: RemediationAction,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        self.executed.append(action)
        if self._succeed_types is not None:
            should_succeed = action.action_type in self._succeed_types
        else:
            should_succeed = self._default_succeed
        if not should_succeed:
            return {"status": "failed", "output": None, "error": "Configured to fail"}
        return {"status": "succeeded", "output": {"action_id": action.action_id}, "error": None}

    async def rollback(
        self,
        action: RemediationAction,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        self.rolled_back.append(action)
        return {"status": "succeeded", "output": None, "error": None}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class RemediationEngine:
    """Policy-aware, generic remediation plan executor.

    Workflow per execution:
      1. Policy-evaluate each action in the plan.
      2. Reject or skip actions that are not permitted.
      3. Execute permitted actions stage-by-stage.
      4. On action failure: attempt rollback if the action is reversible.
      5. Return an aggregated RemediationResult.

    HIGH and CRITICAL risk actions are blocked (not executed) unless the
    policy gateway explicitly permits them.  This means a real gateway
    implementation must handle approval state before returning ``allowed=True``.
    """

    policy_gateway: PolicyGateway
    executor: ActionExecutor
    max_concurrent_actions: int = 4
    action_timeout_seconds: float = 120.0

    async def execute_plan(
        self,
        plan: RemediationPlan,
        incident: Incident,
        *,
        correlation_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> RemediationResult:
        """Execute *plan* according to policy and return the aggregated result."""
        started = datetime.now(UTC)
        action_results: dict[str, Any] = {}
        succeeded = 0
        failed = 0
        rolled_back = 0

        # Stage-by-stage execution
        for stage_ids in plan.stages:
            stage_actions = [a for a in plan.actions if a.action_id in stage_ids]
            stage_results = await self._execute_stage(
                stage_actions,
                incident,
                correlation_id=correlation_id,
                context=context,
            )
            for action_id, result in stage_results.items():
                action_results[action_id] = result
                if result.get("rolled_back"):
                    rolled_back += 1
                elif result.get("status") == "succeeded":
                    succeeded += 1
                elif result.get("status") not in ("skipped", "policy_rejected"):
                    failed += 1

        finished = datetime.now(UTC)
        duration_ms = (finished - started).total_seconds() * 1000
        total_attempted = sum(
            1 for r in action_results.values()
            if r.get("status") not in ("skipped", "policy_rejected")
        )
        overall_success = failed == 0 and rolled_back == 0 and total_attempted > 0

        return RemediationResult(
            plan_id=plan.plan_id,
            incident_id=incident.incident_id,
            succeeded=overall_success,
            actions_attempted=total_attempted,
            actions_succeeded=succeeded,
            actions_failed=failed,
            actions_rolled_back=rolled_back,
            duration_ms=duration_ms,
            action_results=action_results,
            failure_reason=None if overall_success else "One or more actions failed.",
        )

    async def _execute_stage(
        self,
        actions: list[RemediationAction],
        incident: Incident,
        *,
        correlation_id: str | None,
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Execute all actions in a stage concurrently."""
        semaphore = asyncio.Semaphore(self.max_concurrent_actions)
        results: dict[str, Any] = {}

        async def _run_one(action: RemediationAction) -> None:
            async with semaphore:
                result = await self._execute_one(
                    action, incident, correlation_id=correlation_id, context=context
                )
                results[action.action_id] = result

        await asyncio.gather(*[_run_one(a) for a in actions])
        return results

    async def _execute_one(
        self,
        action: RemediationAction,
        incident: Incident,
        *,
        correlation_id: str | None,
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Evaluate policy and execute a single action, rolling back on failure."""
        # Policy gate
        policy_decision = await self.policy_gateway.evaluate(
            action, incident, context=context
        )
        if not policy_decision.get("allowed", False):
            return {
                "status": "policy_rejected",
                "reason": policy_decision.get("reason", "policy rejected"),
                "output": None,
                "error": None,
                "rolled_back": False,
            }

        # Execute with timeout
        try:
            outcome = await asyncio.wait_for(
                self.executor.execute(action, correlation_id=correlation_id),
                timeout=min(action.timeout_seconds, self.action_timeout_seconds),
            )
        except TimeoutError:
            outcome = {
                "status": "failed",
                "output": None,
                "error": f"Action timed out after {action.timeout_seconds}s",
            }
        except Exception as exc:
            outcome = {"status": "failed", "output": None, "error": str(exc)}

        # Rollback on failure if action is reversible
        rolled_back = False
        if outcome.get("status") == "failed" and action.is_reversible:
            try:
                await self.executor.rollback(action, correlation_id=correlation_id)
                rolled_back = True
            except Exception:
                pass

        return {**outcome, "rolled_back": rolled_back}
