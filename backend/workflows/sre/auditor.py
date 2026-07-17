"""WorkflowAuditor — persists audit entries for every significant workflow event."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backend.workflows.sre.exceptions import AuditError
from backend.workflows.sre.models import AuditEntry
from backend.workflows.sre.ports import AuditRepository

_ACTOR = "sre-workflow"


@dataclass(slots=True)
class WorkflowAuditor:
    """Persists audit-log entries to a durable repository.

    Every significant phase transition, approval gate, and recovery action
    produces an entry. Entries are persisted immediately via ``AuditRepository``
    and also accumulated in-memory so the orchestrator can include them in the
    ``SREWorkflowResult``.

    Persistence failures raise ``AuditError`` without interrupting the workflow
    — callers decide whether to treat audit failures as fatal.
    """

    repository: AuditRepository
    _entries: list[AuditEntry] = field(default_factory=list)

    async def record(
        self,
        *,
        incident_id: str,
        phase: str,
        event: str,
        details: dict[str, Any] | None = None,
        workflow_id: str | None = None,
        execution_id: str | None = None,
        actor: str = _ACTOR,
    ) -> AuditEntry:
        """Create, persist, and buffer one audit entry.

        Raises:
            AuditError: If the repository raises during persistence.
        """
        entry = AuditEntry(
            entry_id=str(uuid.uuid4()),
            incident_id=incident_id,
            phase=phase,
            event=event,
            actor=actor,
            timestamp=datetime.now(UTC),
            workflow_id=workflow_id,
            execution_id=execution_id,
            details=details or {},
        )
        try:
            await self.repository.persist(entry)
        except Exception as exc:
            raise AuditError(
                f"Failed to persist audit entry for phase='{phase}' "
                f"event='{event}': {exc}"
            ) from exc
        self._entries.append(entry)
        return entry

    def entries(self) -> tuple[AuditEntry, ...]:
        """Return all entries recorded during this auditor's lifetime."""
        return tuple(self._entries)
