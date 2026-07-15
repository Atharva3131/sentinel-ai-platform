"""Workflow event publishing and Redis Streams integration."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from backend.queues.redis import RedisStreamClient
from backend.workflows.context import WorkflowContext, WorkflowEventEmitter

DEFAULT_WORKFLOW_EVENT_STREAM = "sentinel:workflow:events:v1"

WORKFLOW_EVENT_TYPES: dict[str, str] = {
    "WorkflowCreated": "workflow.created.v1",
    "WorkflowStarted": "workflow.started.v1",
    "WorkflowCompleted": "workflow.completed.v1",
    "WorkflowFailed": "workflow.failed.v1",
    "WorkflowCancelled": "workflow.cancelled.v1",
    "WorkflowTimedOut": "workflow.timed_out.v1",
    "WorkflowRetried": "workflow.retried.v1",
    "WorkflowRecovered": "workflow.recovered.v1",
    "WorkflowCheckpointed": "workflow.checkpointed.v1",
}

_STREAM_EVENT_TO_ALIAS = {
    "workflow.created": "WorkflowCreated",
    "workflow.started": "WorkflowStarted",
    "workflow.completed": "WorkflowCompleted",
    "workflow.failed": "WorkflowFailed",
    "workflow.cancelled": "WorkflowCancelled",
    "workflow.timed_out": "WorkflowTimedOut",
    "workflow.retried": "WorkflowRetried",
    "workflow.recovered": "WorkflowRecovered",
    "workflow.checkpointed": "WorkflowCheckpointed",
}


@dataclass(frozen=True, slots=True)
class WorkflowEventEnvelope:
    """Normalized event envelope written to Redis Streams."""

    event_name: str
    event_type: str
    workflow_id: str
    execution_id: str
    correlation_id: str | None = None
    workflow_version: str | None = None
    attempt: int = 1
    checkpoint_id: str | None = None
    source: str | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    stream: str = DEFAULT_WORKFLOW_EVENT_STREAM
    message_id: str | None = None

    def to_fields(self) -> dict[str, str]:
        """Serialize the envelope into Redis stream fields."""
        return {
            "event_name": self.event_name,
            "event_type": self.event_type,
            "workflow_id": self.workflow_id,
            "execution_id": self.execution_id,
            "correlation_id": self.correlation_id or "",
            "workflow_version": self.workflow_version or "",
            "attempt": str(self.attempt),
            "checkpoint_id": self.checkpoint_id or "",
            "source": self.source or "",
            "occurred_at": self.occurred_at.isoformat(),
            "payload": json.dumps(self.payload, sort_keys=True, default=str),
            "metadata": json.dumps(self.metadata, sort_keys=True, default=str),
        }

    @classmethod
    def from_fields(
        cls,
        *,
        stream: str,
        message_id: str,
        fields: Mapping[str, Any],
    ) -> WorkflowEventEnvelope:
        """Rehydrate an envelope from Redis stream fields."""
        payload = json.loads(str(fields.get("payload", "{}")))
        metadata = json.loads(str(fields.get("metadata", "{}")))
        occurred_at_raw = str(fields.get("occurred_at") or datetime.now(UTC).isoformat())
        occurred_at = datetime.fromisoformat(occurred_at_raw)
        return cls(
            stream=stream,
            message_id=message_id,
            event_name=str(fields.get("event_name", "")),
            event_type=str(fields.get("event_type", "")),
            workflow_id=str(fields.get("workflow_id", "")),
            execution_id=str(fields.get("execution_id", "")),
            correlation_id=_none_if_blank(fields.get("correlation_id")),
            workflow_version=_none_if_blank(fields.get("workflow_version")),
            attempt=int(fields.get("attempt", 1)),
            checkpoint_id=_none_if_blank(fields.get("checkpoint_id")),
            source=_none_if_blank(fields.get("source")),
            occurred_at=occurred_at,
            payload=payload if isinstance(payload, dict) else {"value": payload},
            metadata=metadata if isinstance(metadata, dict) else {"value": metadata},
        )


@runtime_checkable
class WorkflowEventPublisher(Protocol):
    """Contract for publishing workflow lifecycle events."""

    async def publish(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: WorkflowContext,
    ) -> WorkflowEventEnvelope:
        """Publish a workflow event and return the persisted envelope."""
        ...


@runtime_checkable
class WorkflowEventHandler(Protocol):
    """Pluggable consumer contract for workflow event handlers."""

    async def handle(self, event: WorkflowEventEnvelope) -> None:
        """Process one workflow event."""
        ...


@dataclass(slots=True)
class RedisWorkflowEventPublisher(WorkflowEventEmitter):
    """Publish workflow events to a Redis Stream."""

    streams: RedisStreamClient
    stream_name: str = DEFAULT_WORKFLOW_EVENT_STREAM
    source: str = "workflow-executor"

    async def emit(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: WorkflowContext,
    ) -> None:
        """Publish an event for workflow/runtime consumers."""
        await self.publish(event_name, payload, context)

    async def publish(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: WorkflowContext,
    ) -> WorkflowEventEnvelope:
        """Publish an event and return the persisted envelope."""
        event_type = self._event_type(event_name)
        envelope = WorkflowEventEnvelope(
            stream=self.stream_name,
            event_name=event_name,
            event_type=event_type,
            workflow_id=context.workflow_id,
            execution_id=context.execution_id,
            correlation_id=context.correlation_id,
            workflow_version=context.workflow_version,
            attempt=context.attempt,
            checkpoint_id=context.checkpoint_id or _payload_string(payload, "checkpoint_id"),
            source=self.source,
            payload=dict(payload),
            metadata=dict(context.metadata),
        )
        message_id = await self.streams.add(self.stream_name, envelope.to_fields())
        return replace(envelope, message_id=message_id)

    async def publish_workflow_created(
        self,
        payload: Mapping[str, Any],
        context: WorkflowContext,
    ) -> WorkflowEventEnvelope:
        """Publish a `WorkflowCreated` event."""
        return await self.publish("workflow.created", payload, context)

    async def publish_workflow_started(
        self,
        payload: Mapping[str, Any],
        context: WorkflowContext,
    ) -> WorkflowEventEnvelope:
        """Publish a `WorkflowStarted` event."""
        return await self.publish("workflow.started", payload, context)

    async def publish_workflow_completed(
        self,
        payload: Mapping[str, Any],
        context: WorkflowContext,
    ) -> WorkflowEventEnvelope:
        """Publish a `WorkflowCompleted` event."""
        return await self.publish("workflow.completed", payload, context)

    async def publish_workflow_failed(
        self,
        payload: Mapping[str, Any],
        context: WorkflowContext,
    ) -> WorkflowEventEnvelope:
        """Publish a `WorkflowFailed` event."""
        return await self.publish("workflow.failed", payload, context)

    async def publish_workflow_cancelled(
        self,
        payload: Mapping[str, Any],
        context: WorkflowContext,
    ) -> WorkflowEventEnvelope:
        """Publish a `WorkflowCancelled` event."""
        return await self.publish("workflow.cancelled", payload, context)

    async def publish_workflow_timed_out(
        self,
        payload: Mapping[str, Any],
        context: WorkflowContext,
    ) -> WorkflowEventEnvelope:
        """Publish a `WorkflowTimedOut` event."""
        return await self.publish("workflow.timed_out", payload, context)

    async def publish_workflow_retried(
        self,
        payload: Mapping[str, Any],
        context: WorkflowContext,
    ) -> WorkflowEventEnvelope:
        """Publish a `WorkflowRetried` event."""
        return await self.publish("workflow.retried", payload, context)

    async def publish_workflow_recovered(
        self,
        payload: Mapping[str, Any],
        context: WorkflowContext,
    ) -> WorkflowEventEnvelope:
        """Publish a `WorkflowRecovered` event."""
        return await self.publish("workflow.recovered", payload, context)

    async def publish_workflow_checkpointed(
        self,
        payload: Mapping[str, Any],
        context: WorkflowContext,
    ) -> WorkflowEventEnvelope:
        """Publish a `WorkflowCheckpointed` event."""
        return await self.publish("workflow.checkpointed", payload, context)

    def _event_type(self, event_name: str) -> str:
        alias = _STREAM_EVENT_TO_ALIAS.get(event_name)
        if alias is None:
            return event_name
        return WORKFLOW_EVENT_TYPES[alias]


@dataclass(slots=True)
class WorkflowEventStreamConsumer:
    """Pluggable Redis Streams consumer for workflow events."""

    streams: RedisStreamClient
    stream_name: str = DEFAULT_WORKFLOW_EVENT_STREAM
    group_name: str = "workflow-event-consumers"
    consumer_name: str = "workflow-event-consumer"
    block_ms: int = 1000
    batch_size: int = 10

    async def ensure_group(self) -> None:
        """Create the Redis consumer group if needed."""
        try:
            await self.streams.create_consumer_group(self.stream_name, self.group_name)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def receive(self) -> list[WorkflowEventEnvelope]:
        """Receive a batch of workflow events from the stream."""
        records = await self.streams.read(
            {self.stream_name: ">"},
            count=self.batch_size,
            block_ms=self.block_ms,
            group=self.group_name,
            consumer=self.consumer_name,
        )
        return [
            WorkflowEventEnvelope.from_fields(
                stream=record.stream,
                message_id=record.message_id,
                fields=record.payload,
            )
            for record in records
        ]


def _payload_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return None if value is None else str(value)


def _none_if_blank(value: Any) -> str | None:
    if value is None:
        return None
    string_value = str(value)
    return None if string_value == "" else string_value
