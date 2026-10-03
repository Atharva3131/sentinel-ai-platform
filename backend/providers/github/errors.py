"""GitHub-specific error hierarchy.

All errors are domain-neutral — no GitHub API concepts leak into callers.
"""

from __future__ import annotations


class GitHubError(Exception):
    """Base for all GitHub client errors."""

    def __init__(self, message: str, *, operation: str | None = None) -> None:
        super().__init__(message)
        self.operation = operation


class GitHubAuthError(GitHubError):
    """Raised on 401/403 — bad or missing token."""


class GitHubNotFoundError(GitHubError):
    """Raised on 404 — repository, branch, file, or PR not found."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        resource: str | None = None,
    ) -> None:
        super().__init__(message, operation=operation)
        self.resource = resource


class GitHubConflictError(GitHubError):
    """Raised on 409/422 — branch already exists, merge conflict, etc."""


class GitHubRateLimitError(GitHubError):
    """Raised on 429 or X-RateLimit-Remaining: 0."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, operation=operation)
        self.retry_after_seconds = retry_after_seconds


class GitHubUnavailableError(GitHubError):
    """Raised on 5xx or network error."""


class GitHubTimeoutError(GitHubError):
    """Raised when the request exceeds the configured timeout."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        super().__init__(message, operation=operation)
        self.timeout_seconds = timeout_seconds


class GitHubCancelledError(GitHubError):
    """Raised when the cancellation token was set before the request completed."""


class GitHubInvalidInputError(GitHubError):
    """Raised when the caller supplies invalid parameters."""
