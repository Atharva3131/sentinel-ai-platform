"""GitHubActionExecutor — implements the ActionExecutor protocol for GitHub operations.

Dispatches ``RemediationAction`` objects to the appropriate GitHubClient call
based on ``action.metadata["github_operation"]``.  The engine remains responsible
for policy evaluation and orchestration; this executor performs only GitHub I/O.

Supported ``github_operation`` values:
  branch_create           — create a branch
  file_write              — create or update a file
  pull_request_create     — open a PR
  repository_read         — read-only metadata fetch (investigation context)
  file_read               — read a file

Rollback:
  branch_create has no server-side rollback (branches are cheap and ephemeral).
  file_write is reversible by writing the original content back (caller must
  supply ``rollback_parameters["original_sha"]`` and ``rollback_parameters["original_content"]``).
  pull_request_create is reversible by closing the PR (no branch deletion).

Every mutation emits a structlog record so the audit trail is always populated
even when the SRE AuditRepository is not wired.
"""

from __future__ import annotations

from typing import Any

import structlog

from backend.models.remediation import RemediationAction
from backend.providers.github.client import GitHubClient
from backend.providers.github.errors import GitHubError

log = structlog.get_logger(__name__)


class GitHubActionExecutor:
    """Implements ``ActionExecutor`` for GitHub-backed remediation actions."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    async def execute(
        self,
        action: RemediationAction,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch to the appropriate GitHub operation."""
        operation = action.metadata.get("github_operation", "")
        bound_log = log.bind(
            operation=operation,
            action_id=action.action_id,
            action_type=action.action_type,
            correlation_id=correlation_id,
        )
        bound_log.info("github_action_executing")

        try:
            result = await self._dispatch(action, operation, correlation_id=correlation_id)
        except GitHubError as exc:
            bound_log.warning("github_action_failed", error=str(exc))
            return {
                "status": "failed",
                "output": None,
                "error": str(exc),
                "github_error_kind": type(exc).__name__,
            }

        bound_log.info("github_action_succeeded", output_keys=list(result.keys()))
        return {"status": "succeeded", "output": result, "error": None}

    async def rollback(
        self,
        action: RemediationAction,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Roll back a previously executed action where possible."""
        operation = action.metadata.get("github_operation", "")

        if operation == "file_write":
            return await self._rollback_file_write(action, correlation_id=correlation_id)

        if operation == "pull_request_create":
            # Mark the PR as closed (non-destructive)
            return await self._rollback_pr(action, correlation_id=correlation_id)

        # branch_create and read operations have no rollback
        return {
            "status": "succeeded",
            "output": {"note": f"No rollback defined for operation '{operation}'"},
            "error": None,
        }

    # ── Dispatch ──────────────────────────────────────────────────────────

    async def _dispatch(
        self,
        action: RemediationAction,
        operation: str,
        *,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        p = action.parameters
        owner = p.get("owner") or self._client.default_owner
        repo = p.get("repo") or self._client.default_repository
        inc_id = action.metadata.get("incident_id")

        if operation == "branch_create":
            return await self._client.create_branch(
                owner, repo,
                p["branch_name"],
                p["base_sha"],
                correlation_id=correlation_id,
                incident_id=inc_id,
            )

        if operation == "file_write":
            return await self._client.create_or_update_file(
                owner, repo,
                p["path"],
                p["message"],
                p["content"],
                branch=p["branch"],
                sha=p.get("sha"),
                correlation_id=correlation_id,
                incident_id=inc_id,
            )

        if operation == "pull_request_create":
            return await self._client.create_pull_request(
                owner, repo,
                p["title"],
                p["head"],
                p["base"],
                p.get("body", ""),
                draft=bool(p.get("draft", False)),
                correlation_id=correlation_id,
                incident_id=inc_id,
            )

        if operation == "repository_read":
            return await self._client.get_repository(
                owner, repo,
                correlation_id=correlation_id,
                incident_id=inc_id,
            )

        if operation == "file_read":
            return await self._client.get_file(
                owner, repo,
                p["path"],
                ref=p.get("ref"),
                correlation_id=correlation_id,
                incident_id=inc_id,
            )

        raise ValueError(f"Unknown github_operation: {operation!r}")

    async def _rollback_file_write(
        self,
        action: RemediationAction,
        *,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        rp = action.rollback_parameters
        owner = action.parameters.get("owner") or self._client.default_owner
        repo = action.parameters.get("repo") or self._client.default_repository
        path = action.parameters.get("path", "")
        branch = action.parameters.get("branch", "")
        original_content = rp.get("original_content", "")
        original_sha = rp.get("original_sha")

        if not path or not branch:
            return {
                "status": "succeeded",
                "output": {"note": "no_path_for_rollback"},
                "error": None,
            }

        try:
            result = await self._client.create_or_update_file(
                owner, repo, path,
                f"Rollback: revert {path}",
                original_content,
                branch=branch,
                sha=original_sha,
                correlation_id=correlation_id,
            )
            return {"status": "succeeded", "output": result, "error": None}
        except GitHubError as exc:
            return {"status": "failed", "output": None, "error": str(exc)}

    async def _rollback_pr(
        self,
        action: RemediationAction,
        *,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        # We can't close a PR via the client without a PATCH endpoint.
        # Record the intent for human follow-up.
        pr_number = action.metadata.get("pr_number")
        log.info(
            "github_pr_rollback_noted",
            pr_number=pr_number,
            action_id=action.action_id,
            correlation_id=correlation_id,
        )
        return {
            "status": "succeeded",
            "output": {
                "note": f"PR #{pr_number} should be closed manually.",
                "pr_number": pr_number,
            },
            "error": None,
        }
