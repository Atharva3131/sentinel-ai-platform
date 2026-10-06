"""Application-level health aggregation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass

from backend.interfaces.health import HealthCheck, HealthCheckResult, HealthStatus
from backend.telemetry import TelemetryHandle


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Aggregate readiness result for all required dependencies."""

    ready: bool
    checks: tuple[HealthCheckResult, ...]


class HealthService:
    """Run bounded dependency checks concurrently."""

    def __init__(
        self,
        checks: Iterable[HealthCheck],
        timeout_seconds: float,
        telemetry: TelemetryHandle | None = None,
    ) -> None:
        self._checks = tuple(checks)
        self._timeout_seconds = timeout_seconds
        self._telemetry = telemetry

    async def readiness(self) -> ReadinessReport:
        """Return readiness only when every configured dependency is healthy."""
        results = await asyncio.gather(
            *(self._run_check(check) for check in self._checks)
        )
        return ReadinessReport(
            ready=all(result.status is HealthStatus.UP for result in results),
            checks=tuple(results),
        )

    async def _run_check(self, check: HealthCheck) -> HealthCheckResult:
        started = time.perf_counter()

        try:
            async with asyncio.timeout(self._timeout_seconds):
                await check.check()

        except TimeoutError:
            result = self._result(
                check.name,
                HealthStatus.DOWN,
                started,
                "timeout",
            )
            self._record(result)
            return result

        except Exception as exc:  # Dependency failures are normalized at this boundary.
            result = self._result(
                check.name,
                HealthStatus.DOWN,
                started,
                f"{type(exc).__name__}: {exc}",
            )
            self._record(result)
            return result

        result = self._result(check.name, HealthStatus.UP, started)
        self._record(result)
        return result

    @staticmethod
    def _result(
        name: str,
        status: HealthStatus,
        started: float,
        detail: str | None = None,
    ) -> HealthCheckResult:
        result = HealthCheckResult(
            name=name,
            status=status,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            detail=detail,
        )
        return result

    def _record(self, result: HealthCheckResult) -> None:
        if self._telemetry is None:
            return

        self._telemetry.record_health_check(
            name=result.name,
            status=result.status.value,
            duration_ms=result.latency_ms,
        )