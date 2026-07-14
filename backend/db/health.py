"""Database readiness checks."""

from __future__ import annotations

from backend.db.retry import RetryPolicy
from backend.db.session import DatabaseSessionManager


class PostgresHealthCheck:
    """Verify PostgreSQL accepts a minimal query with retry handling."""

    name = "postgresql"

    def __init__(
        self,
        session_manager: DatabaseSessionManager,
        retry_policy: RetryPolicy,
    ) -> None:
        self._session_manager = session_manager
        self._retry_policy = retry_policy

    async def check(self) -> None:
        await self._retry_policy.run(self._session_manager.ping)
