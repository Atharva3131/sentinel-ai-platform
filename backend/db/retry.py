"""Retry policy for transient database failures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

T = TypeVar("T")


@dataclass(slots=True)
class RetryPolicy:
    """Simple exponential-backoff retry policy."""

    max_attempts: int = 3
    initial_delay_seconds: float = 0.2
    max_delay_seconds: float = 2.0
    backoff_factor: float = 2.0
    retryable_exceptions: tuple[type[BaseException], ...] = field(
        default_factory=lambda: (
            OperationalError,
            InterfaceError,
            DBAPIError,
            ConnectionError,
            TimeoutError,
            OSError,
        )
    )
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def should_retry(self, exc: BaseException) -> bool:
        """Return whether the exception is considered transient."""
        return isinstance(exc, self.retryable_exceptions)

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run an async operation with backoff and transient retry handling."""
        delay = self.initial_delay_seconds
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await operation()
            except Exception as exc:
                if attempt >= self.max_attempts or not self.should_retry(exc):
                    raise
                await self.sleep(delay)
                delay = min(delay * self.backoff_factor, self.max_delay_seconds)

        raise RuntimeError("retry policy exhausted without returning")  # pragma: no cover
