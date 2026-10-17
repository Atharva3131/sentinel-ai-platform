"""Demo 1 integration policy and incident matching."""

from __future__ import annotations

from backend.models.incident import Incident


def is_demo1_incident(incident: Incident) -> bool:
    """Return True only for incidents emitted by the Demo 1 Order API.

    The predicate is deliberately strict so ordinary production incidents do
    not enter the observe-only path accidentally.
    """

    correlation_id = incident.correlation_id or ""
    return (
        incident.environment == "demo"
        and "demo1" in incident.tags
        and correlation_id.startswith("demo1-")
    )
