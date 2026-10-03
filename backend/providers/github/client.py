"""Production GitHub API client.

Uses the GitHub REST API v3 over httpx.  No GitHub SDK is imported — all I/O
is plain async HTTP, exactly like the LLM and evidence adapters.

Capabilities:
  - get_repository      — fetch repository metadata
  - get_file            — read a file's content and SHA
  - create_branch       — create a new branch from a base ref
  - create_or_update_file — create or update a file (requires SHA for updates)
  - create_pull_request — open a PR
  - get_pull_request    — fetch PR state
  - get_check_runs      — list CI check runs for a ref

Security:
  * Token is passed as ``Authorization: token <value>``; never logged.
  * No token value appears in OTel attributes, log records, or repr.
  * ``sensitive_headers`` list is excluded from debug output.

Error mapping:
  401 / 403         → GitHubAuthError
  404               → GitHubNotFoundError
  409 / 422         → GitHubConflictError
  429               → GitHubRateLimitError (Retry-After respected)
  5xx               → GitHubUnavailableError
  timeout           → GitHubTimeoutError
  connect error     → GitHubUnavailableError
  cancellation      → GitHubCancelledError
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from backend.configuration.settings import GitHubSettings
from backend.providers.github.errors import (
    GitHubAuthError,
    GitHubCancelledError,
    GitHubConflictError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTimeoutError,
    GitHubUnavailableError,
)

log = structlog.get_logger(__name__)
_tracer = trace.get_tracer("sentinel.github")

_GITHUB_API_VERSION = "2022-11-28"


@dataclass
class GitHubClient:
    """Async GitHub REST API v3 client.

    Instantiate via ``GitHubClient.from_settings(settings)``.
    Token is stored in a private field and never appears in repr.
    """

    base_url: str
    default_owner: str
    default_repository: str
    timeout_seconds: float = 30.0
    max_retries: int = 3
    retry_min_wait: float = 0.5
    retry_max_wait: float = 10.0
    per_page: int = 30
    # Private — never repr'd
    _token: str | None = field(default=None, init=False, repr=False)
    _http: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_settings(cls, settings: GitHubSettings) -> GitHubClient:
        client = cls(
            base_url=settings.base_url,
            default_owner=settings.default_owner,
            default_repository=settings.default_repository,
            timeout_seconds=settings.timeout_seconds,
            max_retries=settings.max_retries,
            retry_min_wait=settings.retry_min_wait_seconds,
            retry_max_wait=settings.retry_max_wait_seconds,
            per_page=settings.per_page,
        )
        client._token = (
            settings.token.get_secret_value() if settings.token else None
        )
        return client

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            headers: dict[str, str] = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            }
            if self._token:
                # "token" scheme for PATs; "Bearer" also works for GH Apps
                headers["Authorization"] = f"token {self._token}"
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self.timeout_seconds),
            )
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    # ── Public API ────────────────────────────────────────────────────────

    async def get_repository(
        self,
        owner: str,
        repo: str,
        *,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        """Return repository metadata."""
        return await self._get(
            f"/repos/{owner}/{repo}",
            operation="get_repository",
            span_attrs={"github.repo": f"{owner}/{repo}"},
            correlation_id=correlation_id,
            incident_id=incident_id,
        )

    async def get_file(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        ref: str | None = None,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        """Return file content and SHA.

        Returns a dict with keys: ``content`` (decoded string), ``sha``,
        ``path``, ``name``, ``size``, ``encoding``.
        """
        params: dict[str, Any] = {}
        if ref:
            params["ref"] = ref
        raw = await self._get(
            f"/repos/{owner}/{repo}/contents/{path}",
            params=params or None,
            operation="get_file",
            span_attrs={"github.repo": f"{owner}/{repo}", "github.path": path},
            correlation_id=correlation_id,
            incident_id=incident_id,
        )
        # Decode base64 content if present
        if raw.get("encoding") == "base64" and raw.get("content"):
            decoded = base64.b64decode(raw["content"]).decode("utf-8", errors="replace")
            raw = {**raw, "content": decoded}
        return raw

    async def create_branch(
        self,
        owner: str,
        repo: str,
        branch_name: str,
        base_sha: str,
        *,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new branch from *base_sha*.

        Returns the created ref object: ``{"ref": ..., "object": {"sha": ...}}``.
        """
        return await self._post(
            f"/repos/{owner}/{repo}/git/refs",
            json={
                "ref": f"refs/heads/{branch_name}",
                "sha": base_sha,
            },
            operation="create_branch",
            span_attrs={
                "github.repo": f"{owner}/{repo}",
                "github.branch": branch_name,
            },
            correlation_id=correlation_id,
            incident_id=incident_id,
        )

    async def create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        message: str,
        content: str,
        *,
        branch: str,
        sha: str | None = None,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a file.

        ``sha`` is required when updating an existing file.
        ``content`` is the raw string (will be base64-encoded before sending).
        Returns the commit and content objects.
        """
        encoded = base64.b64encode(content.encode()).decode()
        body: dict[str, Any] = {
            "message": message,
            "content": encoded,
            "branch": branch,
        }
        if sha:
            body["sha"] = sha

        return await self._put(
            f"/repos/{owner}/{repo}/contents/{path}",
            json=body,
            operation="create_or_update_file",
            span_attrs={
                "github.repo": f"{owner}/{repo}",
                "github.path": path,
                "github.branch": branch,
            },
            correlation_id=correlation_id,
            incident_id=incident_id,
        )

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str = "",
        *,
        draft: bool = False,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        """Open a pull request.  Returns the PR object (number, url, state, etc.)."""
        return await self._post(
            f"/repos/{owner}/{repo}/pulls",
            json={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": draft,
            },
            operation="create_pull_request",
            span_attrs={
                "github.repo": f"{owner}/{repo}",
                "github.pr.head": head,
                "github.pr.base": base,
            },
            correlation_id=correlation_id,
            incident_id=incident_id,
        )

    async def get_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        """Return PR state (number, state, merged, mergeable, etc.)."""
        return await self._get(
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            operation="get_pull_request",
            span_attrs={
                "github.repo": f"{owner}/{repo}",
                "github.pr.number": str(pr_number),
            },
            correlation_id=correlation_id,
            incident_id=incident_id,
        )

    async def get_check_runs(
        self,
        owner: str,
        repo: str,
        ref: str,
        *,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return CI check runs for *ref* (branch name or commit SHA)."""
        body = await self._get(
            f"/repos/{owner}/{repo}/commits/{ref}/check-runs",
            params={"per_page": self.per_page},
            operation="get_check_runs",
            span_attrs={
                "github.repo": f"{owner}/{repo}",
                "github.ref": ref,
            },
            correlation_id=correlation_id,
            incident_id=incident_id,
        )
        runs: list[dict[str, Any]] = body.get("check_runs", [])
        return runs

    async def health_check(self) -> bool:
        """Return True when the GitHub API is reachable with the configured token."""
        try:
            await self._get("/rate_limit", operation="health_check")
            return True
        except Exception:
            return False

    # ── GitHub Actions workflow methods ───────────────────────────────────

    async def trigger_workflow(
        self,
        owner: str,
        repo: str,
        workflow_id: str,
        ref: str,
        *,
        inputs: dict[str, str] | None = None,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> None:
        """Trigger a workflow_dispatch event for *workflow_id* on *ref*.

        GitHub returns 204 No Content on success; raises on error.
        ``inputs`` are passed directly as workflow inputs — callers must
        ensure only explicitly allow-listed keys are included.
        """
        body: dict[str, Any] = {"ref": ref}
        if inputs:
            body["inputs"] = inputs
        await self._post(
            f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches",
            json=body,
            operation="trigger_workflow",
            span_attrs={
                "github.repo": f"{owner}/{repo}",
                "github.workflow": workflow_id,
                "github.ref": ref,
            },
            correlation_id=correlation_id,
            incident_id=incident_id,
        )

    async def list_workflow_runs(
        self,
        owner: str,
        repo: str,
        workflow_id: str,
        *,
        branch: str | None = None,
        status: str | None = None,
        per_page: int | None = None,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent workflow runs for *workflow_id*.

        ``status`` filters by run status: queued, in_progress, completed, etc.
        """
        params: dict[str, Any] = {"per_page": per_page or self.per_page}
        if branch:
            params["branch"] = branch
        if status:
            params["status"] = status
        body = await self._get(
            f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs",
            params=params,
            operation="list_workflow_runs",
            span_attrs={
                "github.repo": f"{owner}/{repo}",
                "github.workflow": workflow_id,
            },
            correlation_id=correlation_id,
            incident_id=incident_id,
        )
        runs: list[dict[str, Any]] = body.get("workflow_runs", [])
        return runs

    async def get_workflow_run(
        self,
        owner: str,
        repo: str,
        run_id: int,
        *,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        """Return metadata for one workflow run by its numeric ID."""
        return await self._get(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}",
            operation="get_workflow_run",
            span_attrs={
                "github.repo": f"{owner}/{repo}",
                "github.run_id": str(run_id),
            },
            correlation_id=correlation_id,
            incident_id=incident_id,
        )

    async def cancel_workflow_run(
        self,
        owner: str,
        repo: str,
        run_id: int,
        *,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> None:
        """Request cancellation of a running workflow run (returns 202)."""
        await self._post(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/cancel",
            operation="cancel_workflow_run",
            span_attrs={
                "github.repo": f"{owner}/{repo}",
                "github.run_id": str(run_id),
            },
            correlation_id=correlation_id,
            incident_id=incident_id,
        )

    async def get_deployments(
        self,
        owner: str,
        repo: str,
        *,
        environment: str | None = None,
        ref: str | None = None,
        per_page: int | None = None,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return repository deployments, optionally filtered by environment or ref."""
        params: dict[str, Any] = {"per_page": per_page or self.per_page}
        if environment:
            params["environment"] = environment
        if ref:
            params["ref"] = ref
        body = await self._get(
            f"/repos/{owner}/{repo}/deployments",
            params=params,
            operation="get_deployments",
            span_attrs={"github.repo": f"{owner}/{repo}"},
            correlation_id=correlation_id,
            incident_id=incident_id,
        )
        # GET /deployments returns a list directly (not wrapped)
        if isinstance(body, list):
            result_list: list[dict[str, Any]] = body
            return result_list
        deploys: list[dict[str, Any]] = body.get("deployments", [])
        return deploys

    # ── HTTP layer ────────────────────────────────────────────────────────

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        operation: str,
        span_attrs: dict[str, str] | None = None,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET", path,
            params=params,
            operation=operation,
            span_attrs=span_attrs,
            correlation_id=correlation_id,
            incident_id=incident_id,
        )

    async def _post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        operation: str,
        span_attrs: dict[str, str] | None = None,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST", path,
            json=json,
            operation=operation,
            span_attrs=span_attrs,
            correlation_id=correlation_id,
            incident_id=incident_id,
        )

    async def _put(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        operation: str,
        span_attrs: dict[str, str] | None = None,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "PUT", path,
            json=json,
            operation=operation,
            span_attrs=span_attrs,
            correlation_id=correlation_id,
            incident_id=incident_id,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        operation: str,
        span_attrs: dict[str, str] | None = None,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        with _tracer.start_as_current_span(
            f"github.{operation}", kind=trace.SpanKind.CLIENT
        ) as span:
            _set_span_attrs(
                span,
                operation=operation,
                path=path,
                extra=span_attrs or {},
                correlation_id=correlation_id,
                incident_id=incident_id,
            )
            t0 = time.monotonic()
            try:
                result = await self._execute_with_retry(
                    method, path, params=params, json_body=json, operation=operation
                )
            except GitHubError:
                span.set_status(StatusCode.ERROR)
                raise
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                raise GitHubUnavailableError(str(exc), operation=operation) from exc

            latency_ms = (time.monotonic() - t0) * 1000
            span.set_attribute("github.latency_ms", round(latency_ms, 2))
            span.set_status(StatusCode.OK)

        log.debug(
            "github_request_complete",
            operation=operation,
            path=path,
            latency_ms=round(latency_ms, 2),
            correlation_id=correlation_id,
        )
        return result

    async def _execute_with_retry(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        operation: str,
    ) -> dict[str, Any]:
        attempt = 0
        wait = self.retry_min_wait

        while True:
            attempt += 1
            try:
                return await self._send_once(
                    method, path, params=params, json_body=json_body, operation=operation
                )
            except GitHubRateLimitError as exc:
                if attempt > self.max_retries:
                    raise
                delay = exc.retry_after_seconds or wait
                log.warning(
                    "github_rate_limited",
                    operation=operation,
                    attempt=attempt,
                    retry_after=delay,
                )
                await asyncio.sleep(delay)
                wait = min(wait * 2.0, self.retry_max_wait)
            except GitHubUnavailableError:
                if attempt > self.max_retries:
                    raise
                log.warning(
                    "github_unavailable_retrying",
                    operation=operation,
                    attempt=attempt,
                    wait=wait,
                )
                await asyncio.sleep(wait)
                wait = min(wait * 2.0, self.retry_max_wait)
            except (
                GitHubAuthError,
                GitHubNotFoundError,
                GitHubConflictError,
                GitHubTimeoutError,
                GitHubCancelledError,
            ):
                raise

    async def _send_once(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        operation: str,
    ) -> dict[str, Any]:
        try:
            response = await asyncio.wait_for(
                self._client().request(method, path, params=params, json=json_body),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            raise GitHubTimeoutError(
                f"GitHub {operation} timed out after {self.timeout_seconds}s",
                operation=operation,
                timeout_seconds=self.timeout_seconds,
            ) from None
        except httpx.ConnectError as exc:
            raise GitHubUnavailableError(
                f"GitHub unreachable: {exc}", operation=operation
            ) from exc
        except httpx.HTTPError as exc:
            raise GitHubUnavailableError(
                f"GitHub HTTP error: {exc}", operation=operation
            ) from exc

        return self._parse_response(response, operation=operation)

    def _parse_response(
        self, response: httpx.Response, *, operation: str
    ) -> dict[str, Any]:
        status = response.status_code

        if status in (401, 403):
            raise GitHubAuthError(
                f"GitHub auth error ({status}) on {operation}",
                operation=operation,
            )
        if status == 404:
            raise GitHubNotFoundError(
                f"GitHub resource not found (404) on {operation}",
                operation=operation,
            )
        if status == 429:
            retry_after_raw = response.headers.get("Retry-After")
            retry_after = float(retry_after_raw) if retry_after_raw else None
            raise GitHubRateLimitError(
                f"GitHub rate limit exceeded on {operation}",
                operation=operation,
                retry_after_seconds=retry_after,
            )
        if status in (409, 422):
            try:
                detail = response.json().get("message", "conflict or validation error")
            except Exception:
                detail = "conflict or validation error"
            raise GitHubConflictError(
                f"GitHub {operation} conflict: {detail}",
                operation=operation,
            )
        if status >= 500:
            raise GitHubUnavailableError(
                f"GitHub server error ({status}) on {operation}",
                operation=operation,
            )
        if status >= 400:
            try:
                detail = response.json().get("message", "client error")
            except Exception:
                detail = "client error"
            raise GitHubUnavailableError(
                f"GitHub {operation} error ({status}): {detail}",
                operation=operation,
            )

        # 201 Created and 204 No Content are both valid
        if status == 204:
            return {}

        try:
            result: dict[str, Any] = response.json()
            return result
        except Exception as exc:
            raise GitHubUnavailableError(
                f"GitHub returned non-JSON response on {operation}",
                operation=operation,
            ) from exc


def _set_span_attrs(
    span: Any,
    *,
    operation: str,
    path: str,
    extra: dict[str, str],
    correlation_id: str | None,
    incident_id: str | None,
) -> None:
    from opentelemetry.trace import NonRecordingSpan
    if isinstance(span, NonRecordingSpan):
        return
    span.set_attribute("github.operation", operation)
    span.set_attribute("github.path", path)
    for k, v in extra.items():
        span.set_attribute(k, v)
    if correlation_id:
        span.set_attribute("correlation.id", correlation_id)
    if incident_id:
        span.set_attribute("incident.id", incident_id)
