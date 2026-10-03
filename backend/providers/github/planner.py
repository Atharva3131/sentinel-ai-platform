"""GitHubRemediationPlanner — builds a RemediationPlan from an RCA.

Produces a staged plan:
  Stage 0 (READ — risk LOW):
    repository_read   — gather repo context for the investigation
  Stage 1 (READ — risk LOW):
    file_read         — read the file(s) that need changing
  Stage 2 (WRITE — risk MEDIUM):
    branch_create     — create remediation branch
  Stage 3 (WRITE — risk MEDIUM):
    file_write        — apply the fix (one action per file)
  Stage 4 (WRITE — risk HIGH):
    pull_request_create — open the PR (requires approval)

All parameters are derived from the RCA and the incident; no hard-coded
values for specific failure modes.

The planner satisfies the ``RemediationPlanner`` protocol from
``backend.core.remediation_engine``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from backend.models.hypothesis import RootCauseAnalysis
from backend.models.incident import Incident
from backend.models.remediation import (
    ActionRiskLevel,
    RemediationAction,
    RemediationActionType,
    RemediationPlan,
)

_PLAN_VERSION = "github-remediation-v1"


class GitHubRemediationPlanner:
    """Produces a GitHub-based RemediationPlan from an RCA.

    Inject:
      ``owner``              — GitHub owner/org (falls back to ``incident.metadata``)
      ``repo``               — Repository name
      ``base_branch``        — Branch to PR against (default: "main")
      ``pr_title_template``  — format string; receives incident_id and root_cause
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        *,
        base_branch: str = "main",
        pr_title_template: str = "fix: automated remediation for incident {incident_id}",
    ) -> None:
        self._owner = owner
        self._repo = repo
        self._base_branch = base_branch
        self._pr_title_template = pr_title_template

    async def plan(
        self,
        incident: Incident,
        rca: RootCauseAnalysis | None,
        *,
        context: dict[str, Any] | None = None,
    ) -> RemediationPlan:
        """Build a staged remediation plan.

        The caller may supply ``context["files_to_change"]`` — a list of dicts:
          ``{"path": "...", "content": "...", "message": "..."}``
        When absent, the plan contains only the read + branch stages (the agent
        is expected to call ``file_write`` tools directly once the file content
        is known).
        """
        ctx = context or {}
        plan_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        inc_id = incident.incident_id
        corr_id = incident.correlation_id or ""

        branch_name = (
            ctx.get("branch_name")
            or f"sentinel-remediation/{inc_id[:8]}"
        )
        base_sha = ctx.get("base_sha", "")
        files_to_change: list[dict[str, Any]] = ctx.get("files_to_change", [])

        root_cause_title = rca.root_cause_title if rca else "unknown"
        pr_title = self._pr_title_template.format(
            incident_id=inc_id,
            root_cause=root_cause_title or "unknown",
        )
        pr_body = self._build_pr_body(incident, rca)

        actions: list[RemediationAction] = []

        # ── Stage 0: read repository metadata ────────────────────────────
        repo_read_id = str(uuid.uuid4())
        actions.append(RemediationAction(
            action_id=repo_read_id,
            action_type=RemediationActionType.CUSTOM,
            target_service=self._repo,
            target_environment=incident.environment or "production",
            risk_level=ActionRiskLevel.LOW,
            title=f"Read repository {self._owner}/{self._repo}",
            description="Fetch repository metadata for investigation context.",
            parameters={"owner": self._owner, "repo": self._repo},
            metadata={
                "github_operation": "repository_read",
                "incident_id": inc_id,
                "correlation_id": corr_id,
            },
            idempotency_key=f"repo-read-{inc_id}",
            timeout_seconds=30.0,
            requires_validation=False,
        ))

        # ── Stage 1: create remediation branch ───────────────────────────
        branch_id = str(uuid.uuid4())
        actions.append(RemediationAction(
            action_id=branch_id,
            action_type=RemediationActionType.CUSTOM,
            target_service=self._repo,
            target_environment=incident.environment or "production",
            risk_level=ActionRiskLevel.MEDIUM,
            title=f"Create remediation branch {branch_name}",
            description=f"Branch from {base_sha or 'default branch HEAD'}.",
            parameters={
                "owner": self._owner,
                "repo": self._repo,
                "branch_name": branch_name,
                "base_sha": base_sha,
            },
            metadata={
                "github_operation": "branch_create",
                "incident_id": inc_id,
                "correlation_id": corr_id,
            },
            idempotency_key=f"branch-{branch_name}-{inc_id}",
            timeout_seconds=30.0,
            requires_validation=False,
        ))

        # ── Stage 2: write files (one action per file) ────────────────────
        write_ids: list[str] = []
        for file_spec in files_to_change:
            file_id = str(uuid.uuid4())
            write_ids.append(file_id)
            actions.append(RemediationAction(
                action_id=file_id,
                action_type=RemediationActionType.CUSTOM,
                target_service=self._repo,
                target_environment=incident.environment or "production",
                risk_level=ActionRiskLevel.MEDIUM,
                title=f"Write {file_spec['path']} on {branch_name}",
                description=file_spec.get("message", f"Apply fix to {file_spec['path']}"),
                parameters={
                    "owner": self._owner,
                    "repo": self._repo,
                    "path": file_spec["path"],
                    "content": file_spec.get("content", ""),
                    "message": file_spec.get("message", f"fix: {file_spec['path']}"),
                    "branch": branch_name,
                    "sha": file_spec.get("sha"),
                },
                rollback_action_type=RemediationActionType.REVERT_CONFIG,
                rollback_parameters={
                    "original_content": file_spec.get("original_content", ""),
                    "original_sha": file_spec.get("sha"),
                },
                metadata={
                    "github_operation": "file_write",
                    "incident_id": inc_id,
                    "correlation_id": corr_id,
                },
                idempotency_key=f"file-{file_spec['path'].replace('/', '-')}-{inc_id}",
                timeout_seconds=30.0,
            ))

        # ── Stage 3: open PR (HIGH risk — requires human approval) ────────
        pr_id = str(uuid.uuid4())
        actions.append(RemediationAction(
            action_id=pr_id,
            action_type=RemediationActionType.CUSTOM,
            target_service=self._repo,
            target_environment=incident.environment or "production",
            risk_level=ActionRiskLevel.HIGH,
            title=pr_title,
            description=pr_body,
            parameters={
                "owner": self._owner,
                "repo": self._repo,
                "title": pr_title,
                "head": branch_name,
                "base": self._base_branch,
                "body": pr_body,
            },
            metadata={
                "github_operation": "pull_request_create",
                "incident_id": inc_id,
                "correlation_id": corr_id,
            },
            idempotency_key=f"pr-{branch_name}-{inc_id}",
            timeout_seconds=30.0,
            requires_validation=True,
        ))

        # Build stages tuple
        stage0 = (repo_read_id,)
        stage1 = (branch_id,)
        stage2 = tuple(write_ids)
        stage3 = (pr_id,)
        stages: tuple[tuple[str, ...], ...] = (stage0, stage1)
        if stage2:
            stages = (*stages, stage2)
        stages = (*stages, stage3)

        return RemediationPlan(
            plan_id=plan_id,
            incident_id=inc_id,
            rca_id=rca.rca_id if rca else None,
            actions=tuple(actions),
            stages=stages,
            created_at=now,
            metadata={
                "planner": _PLAN_VERSION,
                "owner": self._owner,
                "repo": self._repo,
                "branch_name": branch_name,
                "base_branch": self._base_branch,
            },
        )

    @staticmethod
    def _build_pr_body(
        incident: Incident,
        rca: RootCauseAnalysis | None,
    ) -> str:
        lines = [
            f"## Automated Remediation — Incident {incident.incident_id}",
            "",
            f"**Severity:** {incident.severity}",
            f"**Affected services:** {', '.join(incident.affected_services)}",
            "",
        ]
        if incident.symptoms:
            lines += ["**Symptoms:**"]
            lines += [f"- {s}" for s in incident.symptoms[:5]]
            lines.append("")
        if rca and rca.root_cause:
            lines += [
                f"**Root cause:** {rca.root_cause.title}",
                "",
                f"**Confidence:** {rca.confidence:.0%}",
                "",
            ]
        if rca and rca.unresolved_uncertainty:
            lines += [
                f"> ⚠️ {rca.unresolved_uncertainty}",
                "",
            ]
        lines += [
            "---",
            "*This PR was opened automatically by Sentinel AI Platform.*",
            "*Please review before merging.*",
        ]
        return "\n".join(lines)
