"""GitHubValidationRunner — ValidationStrategyRunner for PR and CI check validation.

Implements the ``ValidationStrategyRunner`` protocol for kind=CUSTOM,
allowing the existing ``ValidationEngine`` to check GitHub PR and CI status
as part of the post-remediation validation phase.

Two runner implementations:
  GitHubPRStatusRunner    — checks that the remediation PR is open and not conflicted
  GitHubCheckStatusRunner — checks that all CI check-runs passed on the PR branch

Both produce ``ValidationResult`` objects using the domain's existing
ValidationStrategyKind.CUSTOM kind.  No new domain model is introduced.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from backend.models.validation import (
    ValidationResult,
    ValidationStatus,
    ValidationStrategyKind,
)
from backend.providers.github.client import GitHubClient
from backend.providers.github.errors import GitHubError


class GitHubPRStatusRunner:
    """Validates that a remediation PR exists and is in a mergeable state.

    ``ValidationStrategyConfig.parameters`` must contain:
      ``pr_number``   — int
      ``owner``       — str (optional, falls back to client default)
      ``repo``        — str (optional, falls back to client default)

    PASSED  when PR is open and mergeable (or merged).
    FAILED  when PR is closed without merge, in conflict, or not found.
    ERROR   when GitHub is unreachable.
    """

    kind = ValidationStrategyKind.CUSTOM

    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    async def run(
        self,
        config: Any,   # ValidationStrategyConfig
        *,
        context: dict[str, Any] | None = None,
    ) -> ValidationResult:
        now = datetime.now(UTC)
        params = config.parameters
        pr_number = params.get("pr_number")
        owner = params.get("owner") or self._client.default_owner
        repo = params.get("repo") or self._client.default_repository
        correlation_id = (context or {}).get("correlation_id")
        incident_id = (context or {}).get("incident_id")

        if not pr_number:
            return ValidationResult(
                result_id=str(uuid.uuid4()),
                plan_id="",
                strategy_kind=self.kind,
                target_service=repo,
                status=ValidationStatus.ERROR,
                score=0.0,
                message="GitHubPRStatusRunner requires 'pr_number' in parameters.",
                executed_at=now,
                duration_ms=0.0,
            )

        try:
            pr = await self._client.get_pull_request(
                owner, repo, int(pr_number),
                correlation_id=correlation_id,
                incident_id=incident_id,
            )
        except GitHubError as exc:
            return ValidationResult(
                result_id=str(uuid.uuid4()),
                plan_id="",
                strategy_kind=self.kind,
                target_service=repo,
                status=ValidationStatus.ERROR,
                score=0.0,
                message=f"GitHub error fetching PR #{pr_number}: {exc}",
                executed_at=now,
                duration_ms=0.0,
            )

        state = pr.get("state", "")
        merged = bool(pr.get("merged"))
        mergeable = pr.get("mergeable")

        if merged:
            return _passed(repo, f"PR #{pr_number} has been merged.", now)

        if state == "open" and mergeable is not False:
            return _passed(repo, f"PR #{pr_number} is open and mergeable.", now)

        if state == "closed":
            return ValidationResult(
                result_id=str(uuid.uuid4()),
                plan_id="",
                strategy_kind=ValidationStrategyKind.CUSTOM,
                target_service=repo,
                status=ValidationStatus.FAILED,
                score=0.0,
                message=f"PR #{pr_number} is closed without merge.",
                executed_at=now,
                duration_ms=0.0,
                details={"pr_state": state, "merged": merged},
            )

        if mergeable is False:
            return ValidationResult(
                result_id=str(uuid.uuid4()),
                plan_id="",
                strategy_kind=ValidationStrategyKind.CUSTOM,
                target_service=repo,
                status=ValidationStatus.FAILED,
                score=0.3,
                message=f"PR #{pr_number} has merge conflicts.",
                executed_at=now,
                duration_ms=0.0,
                details={"pr_state": state, "mergeable": mergeable},
            )

        # mergeable is None — GitHub is still computing
        return ValidationResult(
            result_id=str(uuid.uuid4()),
            plan_id="",
            strategy_kind=ValidationStrategyKind.CUSTOM,
            target_service=repo,
            status=ValidationStatus.INDETERMINATE,
            score=0.5,
            message=f"PR #{pr_number} mergeable state is pending.",
            executed_at=now,
            duration_ms=0.0,
            details={"pr_state": state},
        )


class GitHubCheckStatusRunner:
    """Validates that all CI check runs passed on the remediation branch.

    ``ValidationStrategyConfig.parameters`` must contain:
      ``ref``    — branch name or commit SHA
      ``owner``  — str (optional)
      ``repo``   — str (optional)

    PASSED  when all check runs have conclusion=success.
    FAILED  when any check run failed.
    INDETERMINATE when checks are still running.
    """

    kind = ValidationStrategyKind.CUSTOM

    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    async def run(
        self,
        config: Any,
        *,
        context: dict[str, Any] | None = None,
    ) -> ValidationResult:
        now = datetime.now(UTC)
        params = config.parameters
        ref = params.get("ref", "")
        owner = params.get("owner") or self._client.default_owner
        repo = params.get("repo") or self._client.default_repository
        correlation_id = (context or {}).get("correlation_id")
        incident_id = (context or {}).get("incident_id")

        if not ref:
            return ValidationResult(
                result_id=str(uuid.uuid4()),
                plan_id="",
                strategy_kind=self.kind,
                target_service=repo,
                status=ValidationStatus.ERROR,
                score=0.0,
                message="GitHubCheckStatusRunner requires 'ref' in parameters.",
                executed_at=now,
                duration_ms=0.0,
            )

        try:
            runs = await self._client.get_check_runs(
                owner, repo, ref,
                correlation_id=correlation_id,
                incident_id=incident_id,
            )
        except GitHubError as exc:
            return ValidationResult(
                result_id=str(uuid.uuid4()),
                plan_id="",
                strategy_kind=self.kind,
                target_service=repo,
                status=ValidationStatus.ERROR,
                score=0.0,
                message=f"GitHub error fetching check runs for {ref}: {exc}",
                executed_at=now,
                duration_ms=0.0,
            )

        if not runs:
            return ValidationResult(
                result_id=str(uuid.uuid4()),
                plan_id="",
                strategy_kind=self.kind,
                target_service=repo,
                status=ValidationStatus.INDETERMINATE,
                score=0.5,
                message=f"No check runs found for ref '{ref}'.",
                executed_at=now,
                duration_ms=0.0,
            )

        total = len(runs)
        success = sum(1 for r in runs if r.get("conclusion") == "success")
        failure = sum(
            1 for r in runs
            if r.get("conclusion") in ("failure", "cancelled", "timed_out")
        )
        pending = sum(1 for r in runs if r.get("status") != "completed")

        if failure > 0:
            return ValidationResult(
                result_id=str(uuid.uuid4()),
                plan_id="",
                strategy_kind=self.kind,
                target_service=repo,
                status=ValidationStatus.FAILED,
                score=success / total,
                message=(
                    f"{failure}/{total} check run(s) failed on '{ref}'."
                ),
                executed_at=now,
                duration_ms=0.0,
                details={"total": total, "success": success, "failure": failure},
            )

        if pending > 0:
            return ValidationResult(
                result_id=str(uuid.uuid4()),
                plan_id="",
                strategy_kind=self.kind,
                target_service=repo,
                status=ValidationStatus.INDETERMINATE,
                score=success / total,
                message=f"{pending}/{total} check run(s) still pending on '{ref}'.",
                executed_at=now,
                duration_ms=0.0,
                details={"total": total, "success": success, "pending": pending},
            )

        return _passed(
            repo,
            f"All {total} check run(s) passed on '{ref}'.",
            now,
            details={"total": total, "success": success},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _passed(
    service: str,
    message: str,
    now: datetime,
    *,
    details: dict[str, Any] | None = None,
) -> ValidationResult:
    return ValidationResult(
        result_id=str(uuid.uuid4()),
        plan_id="",
        strategy_kind=ValidationStrategyKind.CUSTOM,
        target_service=service,
        status=ValidationStatus.PASSED,
        score=1.0,
        message=message,
        executed_at=now,
        duration_ms=0.0,
        details=details or {},
    )


def build_pr_validation_plan(
    incident_id: str,
    remediation_plan_id: str,
    owner: str,
    repo: str,
    pr_number: int,
    branch_name: str,
    *,
    check_ci: bool = True,
) -> Any:
    """Build a ValidationPlan covering PR status and optionally CI checks.

    Convenience factory — the caller may inject its output directly into
    ``ValidationEngine.validate()``.
    """
    from backend.models.validation import ValidationPlan, ValidationStrategyConfig

    strategies = [
        ValidationStrategyConfig(
            strategy_kind=ValidationStrategyKind.CUSTOM,
            target_service=repo,
            timeout_seconds=30.0,
            parameters={
                "owner": owner,
                "repo": repo,
                "pr_number": pr_number,
            },
        ),
    ]
    if check_ci:
        strategies.append(
            ValidationStrategyConfig(
                strategy_kind=ValidationStrategyKind.CUSTOM,
                target_service=repo,
                timeout_seconds=30.0,
                parameters={
                    "owner": owner,
                    "repo": repo,
                    "ref": branch_name,
                },
            )
        )

    return ValidationPlan(
        plan_id=str(uuid.uuid4()),
        incident_id=incident_id,
        remediation_plan_id=remediation_plan_id,
        strategies=tuple(strategies),
        metadata={
            "github_owner": owner,
            "github_repo": repo,
            "pr_number": pr_number,
            "branch_name": branch_name,
        },
    )
