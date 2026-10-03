"""GitHub remediation tools — seven narrowly-scoped Tool implementations.

Each tool wraps exactly one GitHub API operation.  No tool exposes a generic
"call any GitHub API" interface.  The investigation agent uses these tools
through the existing ToolExecutor/ToolRegistry — it never accesses the
GitHubClient directly.

Tools and their permission requirements:
  repository_read      READ   — fetch repo metadata (for investigation context)
  file_read            READ   — read a file (to inspect current config/code)
  branch_create        WRITE  — create a remediation branch
  file_write           WRITE  — create or update a file on the branch
  pull_request_create  WRITE  — open the remediation PR
  pull_request_status  READ   — check PR state (for validation/polling)
  check_status         READ   — inspect CI check results on a ref

Policy enforcement:
  - WRITE tools verify ``ToolPermission.WRITE in context.permissions`` before
    executing.  A missing permission raises ToolException (not a crash).
  - READ tools verify ``ToolPermission.READ in context.permissions``.
  - Every WRITE tool produces an audit metadata dict in the output so the
    orchestration layer can persist it without knowing the tool internals.
"""

from __future__ import annotations

from typing import Any

import structlog

from backend.providers.github.client import GitHubClient
from backend.providers.github.errors import (
    GitHubConflictError,
    GitHubError,
    GitHubRateLimitError,
)
from backend.tools.context import ToolContext
from backend.tools.exceptions import ToolException
from backend.tools.models import ToolInput, ToolMetadata, ToolPermission
from backend.tools.tool import Tool

log = structlog.get_logger(__name__)
_VERSION = "1.0.0"


def _require_permission(
    context: ToolContext, perm: ToolPermission, tool_name: str
) -> None:
    if perm not in context.permissions:
        raise ToolException(
            f"Tool '{tool_name}' requires permission '{perm}' which is not granted.",
            tool_name=tool_name,
            retryable=False,
        )


def _github_error_to_tool_exception(
    exc: GitHubError, tool_name: str
) -> ToolException:
    retryable = isinstance(exc, (GitHubRateLimitError,))
    kind = type(exc).__name__
    return ToolException(
        str(exc),
        tool_name=tool_name,
        retryable=retryable,
        metadata={"github_error_kind": kind, "github_operation": exc.operation or ""},
    )


def _ctx_ids(context: ToolContext) -> dict[str, str | None]:
    return {
        "correlation_id": context.correlation_id,
        "incident_id": context.metadata.get("incident_id"),
    }


# ---------------------------------------------------------------------------
# 1. RepositoryReadTool
# ---------------------------------------------------------------------------


class RepositoryReadTool(Tool):
    """Fetch repository metadata (description, default branch, visibility, etc.)."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client
        self._metadata = ToolMetadata(
            name="repository_read",
            version=_VERSION,
            description=(
                "Fetch GitHub repository metadata including default branch, "
                "visibility, and description."
            ),
            permissions=(ToolPermission.READ,),
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner (org or user)."},
                    "repo":  {"type": "string", "description": "Repository name."},
                },
                "required": [],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "full_name":       {"type": "string"},
                    "default_branch":  {"type": "string"},
                    "visibility":      {"type": "string"},
                    "description":     {"type": "string"},
                    "html_url":        {"type": "string"},
                },
            },
            labels=("github", "read"),
        )

    @property
    def metadata(self) -> ToolMetadata:
        return self._metadata

    async def execute(self, context: ToolContext, tool_input: ToolInput) -> dict[str, Any]:
        _require_permission(context, ToolPermission.READ, self.name)
        owner = tool_input.payload.get("owner") or self._client.default_owner
        repo = tool_input.payload.get("repo") or self._client.default_repository

        ids = _ctx_ids(context)
        try:
            raw = await self._client.get_repository(
                owner, repo,
                correlation_id=ids["correlation_id"],
                incident_id=ids["incident_id"],
            )
        except GitHubError as exc:
            raise _github_error_to_tool_exception(exc, self.name) from exc

        return {
            "full_name": raw.get("full_name"),
            "default_branch": raw.get("default_branch"),
            "visibility": raw.get("visibility"),
            "description": raw.get("description"),
            "html_url": raw.get("html_url"),
            "owner": owner,
            "repo": repo,
        }


# ---------------------------------------------------------------------------
# 2. FileReadTool
# ---------------------------------------------------------------------------


class FileReadTool(Tool):
    """Read a file from a GitHub repository."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client
        self._metadata = ToolMetadata(
            name="file_read",
            version=_VERSION,
            description="Read a file from a GitHub repository at an optional ref.",
            permissions=(ToolPermission.READ,),
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo":  {"type": "string"},
                    "path":  {"type": "string", "description": "File path in the repo."},
                    "ref":   {"type": "string", "description": "Branch, tag, or commit SHA."},
                },
                "required": ["path"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path":    {"type": "string"},
                    "content": {"type": "string"},
                    "sha":     {"type": "string"},
                    "size":    {"type": "integer"},
                },
            },
            labels=("github", "read"),
        )

    @property
    def metadata(self) -> ToolMetadata:
        return self._metadata

    async def execute(self, context: ToolContext, tool_input: ToolInput) -> dict[str, Any]:
        _require_permission(context, ToolPermission.READ, self.name)
        owner = tool_input.payload.get("owner") or self._client.default_owner
        repo = tool_input.payload.get("repo") or self._client.default_repository
        path = tool_input.payload.get("path", "")
        ref = tool_input.payload.get("ref")

        if not path:
            raise ToolException(
                "file_read requires 'path'", tool_name=self.name, retryable=False
            )

        ids = _ctx_ids(context)
        try:
            raw = await self._client.get_file(
                owner, repo, path, ref=ref,
                correlation_id=ids["correlation_id"],
                incident_id=ids["incident_id"],
            )
        except GitHubError as exc:
            raise _github_error_to_tool_exception(exc, self.name) from exc

        return {
            "path": raw.get("path"),
            "content": raw.get("content", ""),
            "sha": raw.get("sha"),
            "size": raw.get("size"),
            "owner": owner,
            "repo": repo,
        }


# ---------------------------------------------------------------------------
# 3. BranchCreateTool
# ---------------------------------------------------------------------------


class BranchCreateTool(Tool):
    """Create a new branch from a base SHA or branch name."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client
        self._metadata = ToolMetadata(
            name="branch_create",
            version=_VERSION,
            description=(
                "Create a new branch in a GitHub repository. "
                "Returns the new branch ref and SHA."
            ),
            permissions=(ToolPermission.WRITE,),
            input_schema={
                "type": "object",
                "properties": {
                    "owner":       {"type": "string"},
                    "repo":        {"type": "string"},
                    "branch_name": {"type": "string", "description": "New branch name."},
                    "base_sha":    {"type": "string", "description": "SHA to branch from."},
                },
                "required": ["branch_name", "base_sha"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "branch_name": {"type": "string"},
                    "sha":         {"type": "string"},
                    "ref":         {"type": "string"},
                },
            },
            labels=("github", "write"),
        )

    @property
    def metadata(self) -> ToolMetadata:
        return self._metadata

    async def execute(self, context: ToolContext, tool_input: ToolInput) -> dict[str, Any]:
        _require_permission(context, ToolPermission.WRITE, self.name)
        owner = tool_input.payload.get("owner") or self._client.default_owner
        repo = tool_input.payload.get("repo") or self._client.default_repository
        branch_name = tool_input.payload.get("branch_name", "")
        base_sha = tool_input.payload.get("base_sha", "")

        if not branch_name or not base_sha:
            raise ToolException(
                "branch_create requires 'branch_name' and 'base_sha'",
                tool_name=self.name, retryable=False,
            )

        ids = _ctx_ids(context)
        try:
            raw = await self._client.create_branch(
                owner, repo, branch_name, base_sha,
                correlation_id=ids["correlation_id"],
                incident_id=ids["incident_id"],
            )
        except GitHubConflictError:
            # Branch already exists — treat as idempotent success
            return {
                "branch_name": branch_name,
                "sha": base_sha,
                "ref": f"refs/heads/{branch_name}",
                "already_existed": True,
                "owner": owner,
                "repo": repo,
            }
        except GitHubError as exc:
            raise _github_error_to_tool_exception(exc, self.name) from exc

        sha = raw.get("object", {}).get("sha", base_sha)
        return {
            "branch_name": branch_name,
            "sha": sha,
            "ref": raw.get("ref"),
            "already_existed": False,
            "owner": owner,
            "repo": repo,
            "_audit": {
                "operation": "branch_create",
                "branch": branch_name,
                "base_sha": base_sha,
                "correlation_id": ids["correlation_id"],
            },
        }


# ---------------------------------------------------------------------------
# 4. FileWriteTool
# ---------------------------------------------------------------------------


class FileWriteTool(Tool):
    """Create or update a file on a branch."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client
        self._metadata = ToolMetadata(
            name="file_write",
            version=_VERSION,
            description=(
                "Create or update a file in a GitHub repository on a named branch. "
                "Provide 'sha' to update an existing file."
            ),
            permissions=(ToolPermission.WRITE,),
            input_schema={
                "type": "object",
                "properties": {
                    "owner":   {"type": "string"},
                    "repo":    {"type": "string"},
                    "path":    {"type": "string"},
                    "content": {"type": "string", "description": "New file content (plain text)."},
                    "message": {"type": "string", "description": "Commit message."},
                    "branch":  {"type": "string"},
                    "sha":     {
                        "type": "string",
                        "description": "Existing file SHA (for updates).",
                    },
                },
                "required": ["path", "content", "message", "branch"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path":       {"type": "string"},
                    "commit_sha": {"type": "string"},
                    "branch":     {"type": "string"},
                    "html_url":   {"type": "string"},
                },
            },
            labels=("github", "write"),
        )

    @property
    def metadata(self) -> ToolMetadata:
        return self._metadata

    async def execute(self, context: ToolContext, tool_input: ToolInput) -> dict[str, Any]:
        _require_permission(context, ToolPermission.WRITE, self.name)
        owner = tool_input.payload.get("owner") or self._client.default_owner
        repo = tool_input.payload.get("repo") or self._client.default_repository
        path = tool_input.payload.get("path", "")
        content = tool_input.payload.get("content", "")
        message = tool_input.payload.get("message", "")
        branch = tool_input.payload.get("branch", "")
        sha = tool_input.payload.get("sha")  # optional — required for updates

        if not path or not message or not branch:
            raise ToolException(
                "file_write requires 'path', 'message', and 'branch'",
                tool_name=self.name, retryable=False,
            )

        ids = _ctx_ids(context)
        try:
            raw = await self._client.create_or_update_file(
                owner, repo, path, message, content,
                branch=branch, sha=sha,
                correlation_id=ids["correlation_id"],
                incident_id=ids["incident_id"],
            )
        except GitHubError as exc:
            raise _github_error_to_tool_exception(exc, self.name) from exc

        commit = raw.get("commit", {})
        content_obj = raw.get("content", {})
        return {
            "path": path,
            "commit_sha": commit.get("sha"),
            "branch": branch,
            "html_url": content_obj.get("html_url"),
            "owner": owner,
            "repo": repo,
            "_audit": {
                "operation": "file_write",
                "path": path,
                "branch": branch,
                "commit_message": message,
                "correlation_id": ids["correlation_id"],
            },
        }


# ---------------------------------------------------------------------------
# 5. PullRequestCreateTool
# ---------------------------------------------------------------------------


class PullRequestCreateTool(Tool):
    """Open a pull request from head to base branch."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client
        self._metadata = ToolMetadata(
            name="pull_request_create",
            version=_VERSION,
            description="Create a pull request from a head branch to a base branch.",
            permissions=(ToolPermission.WRITE,),
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo":  {"type": "string"},
                    "title": {"type": "string"},
                    "head":  {"type": "string", "description": "Head branch name."},
                    "base":  {"type": "string", "description": "Base branch name."},
                    "body":  {"type": "string"},
                    "draft": {"type": "boolean"},
                },
                "required": ["title", "head", "base"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "pr_number": {"type": "integer"},
                    "pr_url":    {"type": "string"},
                    "state":     {"type": "string"},
                    "head":      {"type": "string"},
                    "base":      {"type": "string"},
                },
            },
            labels=("github", "write"),
        )

    @property
    def metadata(self) -> ToolMetadata:
        return self._metadata

    async def execute(self, context: ToolContext, tool_input: ToolInput) -> dict[str, Any]:
        _require_permission(context, ToolPermission.WRITE, self.name)
        owner = tool_input.payload.get("owner") or self._client.default_owner
        repo = tool_input.payload.get("repo") or self._client.default_repository
        title = tool_input.payload.get("title", "")
        head = tool_input.payload.get("head", "")
        base = tool_input.payload.get("base", "")
        body = tool_input.payload.get("body", "")
        draft = bool(tool_input.payload.get("draft", False))

        if not title or not head or not base:
            raise ToolException(
                "pull_request_create requires 'title', 'head', 'base'",
                tool_name=self.name, retryable=False,
            )

        ids = _ctx_ids(context)
        try:
            raw = await self._client.create_pull_request(
                owner, repo, title, head, base, body,
                draft=draft,
                correlation_id=ids["correlation_id"],
                incident_id=ids["incident_id"],
            )
        except GitHubConflictError:
            # PR already exists — still useful; surface the conflict as a detail
            raise ToolException(
                f"PR from '{head}' to '{base}' already exists or conflicts.",
                tool_name=self.name, retryable=False,
                metadata={"github_error_kind": "GitHubConflictError"},
            ) from None
        except GitHubError as exc:
            raise _github_error_to_tool_exception(exc, self.name) from exc

        return {
            "pr_number": raw.get("number"),
            "pr_url": raw.get("html_url"),
            "state": raw.get("state"),
            "head": raw.get("head", {}).get("ref", head),
            "base": raw.get("base", {}).get("ref", base),
            "title": raw.get("title"),
            "owner": owner,
            "repo": repo,
            "_audit": {
                "operation": "pull_request_create",
                "pr_number": raw.get("number"),
                "head": head,
                "base": base,
                "correlation_id": ids["correlation_id"],
            },
        }


# ---------------------------------------------------------------------------
# 6. PullRequestStatusTool
# ---------------------------------------------------------------------------


class PullRequestStatusTool(Tool):
    """Inspect the state of an existing pull request."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client
        self._metadata = ToolMetadata(
            name="pull_request_status",
            version=_VERSION,
            description="Return the current state, merge status, and review state of a PR.",
            permissions=(ToolPermission.READ,),
            input_schema={
                "type": "object",
                "properties": {
                    "owner":     {"type": "string"},
                    "repo":      {"type": "string"},
                    "pr_number": {"type": "integer"},
                },
                "required": ["pr_number"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "pr_number": {"type": "integer"},
                    "state":     {"type": "string"},
                    "merged":    {"type": "boolean"},
                    "mergeable": {"type": "boolean"},
                    "pr_url":    {"type": "string"},
                    "title":     {"type": "string"},
                },
            },
            labels=("github", "read"),
        )

    @property
    def metadata(self) -> ToolMetadata:
        return self._metadata

    async def execute(self, context: ToolContext, tool_input: ToolInput) -> dict[str, Any]:
        _require_permission(context, ToolPermission.READ, self.name)
        owner = tool_input.payload.get("owner") or self._client.default_owner
        repo = tool_input.payload.get("repo") or self._client.default_repository
        pr_number = tool_input.payload.get("pr_number")

        if pr_number is None:
            raise ToolException(
                "pull_request_status requires 'pr_number'",
                tool_name=self.name, retryable=False,
            )

        ids = _ctx_ids(context)
        try:
            raw = await self._client.get_pull_request(
                owner, repo, int(pr_number),
                correlation_id=ids["correlation_id"],
                incident_id=ids["incident_id"],
            )
        except GitHubError as exc:
            raise _github_error_to_tool_exception(exc, self.name) from exc

        return {
            "pr_number": raw.get("number"),
            "state": raw.get("state"),
            "merged": bool(raw.get("merged")),
            "mergeable": raw.get("mergeable"),
            "pr_url": raw.get("html_url"),
            "title": raw.get("title"),
            "draft": bool(raw.get("draft")),
            "owner": owner,
            "repo": repo,
        }


# ---------------------------------------------------------------------------
# 7. CheckStatusTool
# ---------------------------------------------------------------------------


class CheckStatusTool(Tool):
    """Return CI check-run results for a branch or commit ref."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client
        self._metadata = ToolMetadata(
            name="check_status",
            version=_VERSION,
            description=(
                "Return the CI check run results for a branch or commit SHA. "
                "Summarises conclusion counts (success / failure / pending)."
            ),
            permissions=(ToolPermission.READ,),
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo":  {"type": "string"},
                    "ref":   {"type": "string", "description": "Branch name or commit SHA."},
                },
                "required": ["ref"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "ref":             {"type": "string"},
                    "total":           {"type": "integer"},
                    "success_count":   {"type": "integer"},
                    "failure_count":   {"type": "integer"},
                    "pending_count":   {"type": "integer"},
                    "all_passed":      {"type": "boolean"},
                    "check_runs":      {"type": "array"},
                },
            },
            labels=("github", "read"),
        )

    @property
    def metadata(self) -> ToolMetadata:
        return self._metadata

    async def execute(self, context: ToolContext, tool_input: ToolInput) -> dict[str, Any]:
        _require_permission(context, ToolPermission.READ, self.name)
        owner = tool_input.payload.get("owner") or self._client.default_owner
        repo = tool_input.payload.get("repo") or self._client.default_repository
        ref = tool_input.payload.get("ref", "")

        if not ref:
            raise ToolException(
                "check_status requires 'ref'", tool_name=self.name, retryable=False
            )

        ids = _ctx_ids(context)
        try:
            runs = await self._client.get_check_runs(
                owner, repo, ref,
                correlation_id=ids["correlation_id"],
                incident_id=ids["incident_id"],
            )
        except GitHubError as exc:
            raise _github_error_to_tool_exception(exc, self.name) from exc

        success = sum(1 for r in runs if r.get("conclusion") == "success")
        failure = sum(
            1 for r in runs if r.get("conclusion") in ("failure", "cancelled", "timed_out")
        )
        pending = sum(1 for r in runs if r.get("status") != "completed")

        return {
            "ref": ref,
            "total": len(runs),
            "success_count": success,
            "failure_count": failure,
            "pending_count": pending,
            "all_passed": failure == 0 and pending == 0 and len(runs) > 0,
            "check_runs": [
                {
                    "name": r.get("name"),
                    "status": r.get("status"),
                    "conclusion": r.get("conclusion"),
                    "html_url": r.get("html_url"),
                }
                for r in runs
            ],
            "owner": owner,
            "repo": repo,
        }


# ---------------------------------------------------------------------------
# Tool registry helper
# ---------------------------------------------------------------------------


def register_github_tools(
    registry: Any,
    client: GitHubClient,
) -> None:
    """Register all 7 GitHub tools into a ToolRegistry."""
    from backend.tools.registry import ToolProvider, ToolRegistry
    assert isinstance(registry, ToolRegistry)

    tool_instances: list[Tool] = [
        RepositoryReadTool(client),
        FileReadTool(client),
        BranchCreateTool(client),
        FileWriteTool(client),
        PullRequestCreateTool(client),
        PullRequestStatusTool(client),
        CheckStatusTool(client),
    ]

    def _make_provider(t: Tool) -> ToolProvider:
        def _provider(_deps: Any) -> Tool:
            return t
        return _provider

    for tool_instance in tool_instances:
        registry.register(
            name=tool_instance.name,
            version=_VERSION,
            provider=_make_provider(tool_instance),
        )
