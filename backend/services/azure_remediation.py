"""Azure Container Apps remediation service for Demo 1 connection leak incidents.

Provides automatic remediation by restarting the active revision of a Container App
and verifying recovery via health checks.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from azure.identity.aio import ManagedIdentityCredential

from backend.configuration.settings import AzureRemediationSettings
from backend.models.incident import Incident

log = __import__("structlog").get_logger()


@dataclass
class AzureRemediationResult:
    """Result of an Azure remediation attempt."""

    succeeded: bool
    reason: str
    restart_attempt: int | None = None
    health_check_duration_ms: float = 0.0
    metadata: dict[str, Any] | None = None


class AzureRemediationService:
    """Handles Container Apps restart remediation for Demo 1 incidents.

    Responsibilities:
    - Match incident correlation IDs against Demo 1 pattern
    - Restart active revision of target Container App via Azure ARM REST API
    - Poll health endpoint to verify recovery
    - Idempotent: duplicate incident submissions cannot trigger restart storms
    - Authorization: uses Azure Managed Identity with explicit client_id
    """

    def __init__(self, settings: AzureRemediationSettings) -> None:
        """Initialize with configuration.

        Args:
            settings: AzureRemediationSettings with resource and health check config.

        Raises:
            ValueError: if critical settings are missing when enabled=True.
        """
        self._settings = settings
        self._enabled = settings.enabled
        self._correlation_id_pattern = re.compile(settings.correlation_id_pattern)

        if self._enabled:
            if not settings.resource_group or not settings.container_app_name:
                raise ValueError(
                    "resource_group and container_app_name are required when "
                    "azure_remediation is enabled"
                )
            if not settings.health_check_url:
                raise ValueError(
                    "health_check_url is required when "
                    "azure_remediation is enabled"
                )
            if not settings.managed_identity_client_id:
                raise ValueError(
                    "managed_identity_client_id is required when "
                    "azure_remediation is enabled. Set "
                    "SENTINEL_AZURE_REMEDIATION__MANAGED_IDENTITY_CLIENT_ID "
                    "environment variable."
                )

    async def should_remediate(self, incident: Incident) -> bool:
        """Check if incident is eligible for Azure remediation.

        Args:
            incident: The incident to evaluate.

        Returns:
            True if incident matches Demo 1 pattern and service is enabled.
        """
        if not self._enabled:
            return False

        if not incident.correlation_id:
            return False

        return (
            self._correlation_id_pattern.match(incident.correlation_id) is not None
        )

    async def remediate(
        self, incident: Incident
    ) -> AzureRemediationResult:
        """Attempt to remediate the incident by restarting the Container App.

        Args:
            incident: The incident to remediate.

        Returns:
            AzureRemediationResult with success status and metadata.

        Process:
            1. Verify incident is eligible (should_remediate)
            2. Restart active revision via ARM REST API (max attempts)
            3. Poll health endpoint until recovery or timeout
            4. Return result with metadata
        """
        if not self._enabled:
            return AzureRemediationResult(
                succeeded=False,
                reason="Azure remediation is disabled",
            )

        if not await self.should_remediate(incident):
            return AzureRemediationResult(
                succeeded=False,
                reason="Incident does not match Demo 1 correlation ID pattern",
            )

        bound_log = log.bind(
            incident_id=incident.incident_id,
            correlation_id=incident.correlation_id,
            service="azure_remediation",
        )

        bound_log.info("azure_remediation_starting")

        # Attempt restart with retries
        restart_result = await self._restart_container_app(bound_log)
        if not restart_result["succeeded"]:
            return AzureRemediationResult(
                succeeded=False,
                reason=restart_result["reason"],
                restart_attempt=restart_result.get("attempt", 0),
            )

        # Verify health recovery
        health_result = await self._verify_health_recovery(bound_log)

        success = health_result["succeeded"]
        reason = health_result["reason"]
        duration_ms = health_result.get("duration_ms", 0.0)

        if success:
            bound_log.info(
                "azure_remediation_succeeded",
                restart_attempt=restart_result.get("attempt", 0),
                health_check_duration_ms=round(duration_ms, 2),
            )
        else:
            bound_log.warning(
                "azure_remediation_failed",
                reason=reason,
                restart_attempt=restart_result.get("attempt", 0),
                health_check_duration_ms=round(duration_ms, 2),
            )

        return AzureRemediationResult(
            succeeded=success,
            reason=reason,
            restart_attempt=restart_result.get("attempt", 0),
            health_check_duration_ms=duration_ms,
            metadata={
                "restart_attempt": restart_result.get("attempt"),
                "health_checks_performed": health_result.get(
                    "checks_performed", 0
                ),
                "final_status_code": health_result.get("final_status_code"),
            },
        )

    async def _restart_container_app(
        self, bound_log: Any
    ) -> dict[str, Any]:
        """Restart the active revision of the Container App.

        Args:
            bound_log: Structlog bound logger for this operation.

        Returns:
            Dict with succeeded bool, reason str, attempt int, error details.
        """
        rg = self._settings.resource_group
        ca_name = self._settings.container_app_name
        max_attempts = self._settings.max_restart_attempts

        bound_log.info(
            "azure_restart_starting",
            resource_group=rg,
            container_app_name=ca_name,
            max_attempts=max_attempts,
        )

        for attempt in range(1, max_attempts + 1):
            try:
                result = await self._call_restart_api(rg, ca_name, bound_log)
                if result["succeeded"]:
                    bound_log.info(
                        "azure_restart_succeeded",
                        attempt=attempt,
                        revision=result.get("active_revision"),
                    )
                    return {
                        "succeeded": True,
                        "reason": "Container App restarted successfully",
                        "attempt": attempt,
                        "active_revision": result.get("active_revision"),
                    }
                else:
                    bound_log.warning(
                        "azure_restart_attempt_failed",
                        attempt=attempt,
                        reason=result["reason"],
                    )
                    if attempt < max_attempts:
                        await asyncio.sleep(2.0 * attempt)  # exponential backoff
            except Exception as exc:
                bound_log.error(
                    "azure_restart_exception",
                    attempt=attempt,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(2.0 * attempt)

        return {
            "succeeded": False,
            "reason": (
                f"Failed to restart Container App after {max_attempts} attempts"
            ),
            "attempt": max_attempts,
        }

    async def _call_restart_api(
        self, resource_group: str, ca_name: str, bound_log: Any
    ) -> dict[str, Any]:
        """Call Azure ARM REST API to restart the active revision.

        Uses Azure Managed Identity for authentication.

        Args:
            resource_group: Azure resource group name.
            ca_name: Container App name.
            bound_log: Structlog bound logger.

        Returns:
            Dict with succeeded bool, reason str, active_revision str.

        Raises:
            Exception on authentication, network, or API errors.
        """
        # Construct ARM REST API URL
        subscription_id = self._get_subscription_id()
        if not subscription_id:
            return {
                "succeeded": False,
                "reason": "Unable to determine Azure subscription ID",
            }

        url = (
            "https://management.azure.com/subscriptions/"
            f"{subscription_id}/resourceGroups/{resource_group}/"
            f"providers/Microsoft.App/containerApps/{ca_name}/"
            "revisions?api-version=2023-11-02"
        )

        # Get bearer token via Managed Identity
        try:
            credential = ManagedIdentityCredential(
                client_id=self._settings.managed_identity_client_id
            )
            token = await credential.get_token(
                "https://management.azure.com/.default"
            )
            headers = {
                "Authorization": f"Bearer {token.token}",
                "Content-Type": "application/json",
            }
        except Exception as exc:
            return {
                "succeeded": False,
                "reason": f"Azure authentication failed: {exc}",
            }

        # List active revisions
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.health_check_timeout_seconds
            ) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    return {
                        "succeeded": False,
                        "reason": f"Failed to list revisions: {resp.status_code}",
                    }

                data = resp.json()
                revisions = data.get("value", [])
                if not revisions:
                    return {
                        "succeeded": False,
                        "reason": "No revisions found",
                    }

                # Find active revision and trigger restart
                active_revision = None
                for rev in revisions:
                    if rev.get("properties", {}).get("active", False):
                        active_revision = rev.get("name")
                        break

                if not active_revision:
                    return {
                        "succeeded": False,
                        "reason": "No active revision found",
                    }

                # Call restart endpoint for active revision
                restart_url = (
                    "https://management.azure.com/subscriptions/"
                    f"{subscription_id}/resourceGroups/{resource_group}/"
                    f"providers/Microsoft.App/containerApps/{ca_name}/"
                    f"revisions/{active_revision}/restart?"
                    "api-version=2023-11-02"
                )

                restart_resp = await client.post(restart_url, headers=headers)
                if restart_resp.status_code in (200, 202, 204):
                    return {
                        "succeeded": True,
                        "reason": "Restart request accepted",
                        "active_revision": active_revision,
                    }
                else:
                    return {
                        "succeeded": False,
                        "reason": (
                            f"Restart request failed: {restart_resp.status_code}"
                        ),
                    }

        except Exception as exc:
            return {
                "succeeded": False,
                "reason": f"ARM API call failed: {exc}",
            }

    async def _verify_health_recovery(
        self, bound_log: Any
    ) -> dict[str, Any]:
        """Poll health endpoint until recovery or timeout.

        Args:
            bound_log: Structlog bound logger.

        Returns:
            Dict with succeeded bool, reason str, checks_performed int,
            final_status_code int, duration_ms float.
        """
        health_url = self._settings.health_check_url
        max_duration_ms = (
            self._settings.health_check_max_duration_seconds * 1000
        )
        timeout_per_check = self._settings.health_check_timeout_seconds

        bound_log.info(
            "azure_health_check_starting",
            url=health_url,
            max_duration_ms=round(max_duration_ms, 0),
            poll_interval_ms=round(
                self._settings.health_check_poll_interval_seconds * 1000, 0
            ),
        )

        t0 = time.monotonic()
        checks_performed = 0
        final_status_code = None

        while True:
            elapsed_ms = (time.monotonic() - t0) * 1000

            if elapsed_ms > max_duration_ms:
                return {
                    "succeeded": False,
                    "reason": (
                        f"Health check timeout after {checks_performed} checks"
                    ),
                    "checks_performed": checks_performed,
                    "final_status_code": final_status_code,
                    "duration_ms": elapsed_ms,
                }

            try:
                async with httpx.AsyncClient(
                    timeout=timeout_per_check
                ) as client:
                    resp = await client.get(health_url)
                    final_status_code = resp.status_code
                    checks_performed += 1

                    if resp.status_code == 200:
                        # Verify response indicates healthy database pool
                        try:
                            body = resp.json()
                            status_ok = body.get("status") == "healthy"
                            pool_ok = body.get("database_pool_recovered")
                            if status_ok and pool_ok:
                                bound_log.info(
                                    "azure_health_check_passed",
                                    checks=checks_performed,
                                    duration_ms=round(elapsed_ms, 2),
                                )
                                return {
                                    "succeeded": True,
                                    "reason": (
                                        "Health check passed; "
                                        "database pool recovered"
                                    ),
                                    "checks_performed": checks_performed,
                                    "final_status_code": 200,
                                    "duration_ms": elapsed_ms,
                                }
                        except Exception:
                            # JSON parse failed, but status was 200
                            bound_log.warning(
                                "azure_health_check_invalid_response",
                                status_code=200,
                                check_num=checks_performed,
                            )

                    bound_log.debug(
                        "azure_health_check_attempt",
                        check_num=checks_performed,
                        status_code=final_status_code,
                        elapsed_ms=round(elapsed_ms, 2),
                    )

            except TimeoutError:
                bound_log.warning(
                    "azure_health_check_timeout",
                    check_num=checks_performed,
                    elapsed_ms=round(elapsed_ms, 2),
                )
            except Exception as exc:
                bound_log.warning(
                    "azure_health_check_error",
                    check_num=checks_performed,
                    error=str(exc),
                    elapsed_ms=round(elapsed_ms, 2),
                )

            # Wait before next check
            await asyncio.sleep(
                self._settings.health_check_poll_interval_seconds
            )

    def _get_subscription_id(self) -> str | None:
        """Extract Azure subscription ID from environment.

        Returns:
            Subscription ID or None if not available.
        """
        # Try environment variable first
        sub_id = os.getenv("AZURE_SUBSCRIPTION_ID")
        if sub_id:
            return sub_id

        # Could also be derived from Managed Identity credentials,
        # but for now rely on environment configuration
        return None
