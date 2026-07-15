"""Async worker for Redis Stream workflow events."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backend.events.workflow_streams import WorkflowEventEnvelope, WorkflowEventHandler
from backend.queues.redis import RedisDeadLetterQueue, RedisRetryQueue, RedisStreamClient


@dataclass(slots=True)
class WorkflowEventWorker:
    """Consume workflow events with backpressure, retry, and DLQ support."""

    streams: RedisStreamClient
    handler: WorkflowEventHandler
    retry_queue: RedisRetryQueue
    dead_letter_queue: RedisDeadLetterQueue
    stream_name: str
    consumer_group: str
    consumer_name: str
    max_in_flight: int = 8
    batch_size: int = 10
    block_ms: int = 1000
    max_retries: int = 3
    retry_delay_seconds: float = 0.0
    stop_event: asyncio.Event | None = None
    _semaphore: asyncio.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.max_in_flight)

    async def run(self) -> None:
        """Run the worker loop until stopped."""
        await self._ensure_group()
        while self.stop_event is None or not self.stop_event.is_set():
            records = await self.streams.read(
                {self.stream_name: ">"},
                count=self.batch_size,
                block_ms=self.block_ms,
                group=self.consumer_group,
                consumer=self.consumer_name,
            )
            if not records:
                continue
            async with asyncio.TaskGroup() as task_group:
                for record in records:
                    await self._semaphore.acquire()
                    task_group.create_task(
                        self._process_record(
                            record.stream,
                            record.message_id,
                            record.payload,
                        )
                    )

    async def handle(self, event: WorkflowEventEnvelope) -> None:
        """Handle a single event through the injected handler."""
        await self.handler.handle(event)

    async def _ensure_group(self) -> None:
        try:
            await self.streams.create_consumer_group(self.stream_name, self.consumer_group)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _process_record(
        self,
        stream: str,
        message_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        try:
            event = WorkflowEventEnvelope.from_fields(
                stream=stream,
                message_id=message_id,
                fields=payload,
            )
            await self.handle(event)
            await self.streams.ack(stream, self.consumer_group, message_id)
        except Exception as exc:
            await self._handle_failure(stream, message_id, payload, exc)
            await self.streams.ack(stream, self.consumer_group, message_id)
        finally:
            self._semaphore.release()

    async def _handle_failure(
        self,
        stream: str,
        message_id: str,
        payload: Mapping[str, Any],
        exc: Exception,
    ) -> None:
        event = WorkflowEventEnvelope.from_fields(
            stream=stream,
            message_id=message_id,
            fields=payload,
        )
        retry_count = int(event.metadata.get("retry_count", 0))
        error_payload = {
            "event": event.event_name,
            "message_id": message_id,
            "error": type(exc).__name__,
            "error_message": str(exc),
            "workflow_id": event.workflow_id,
            "execution_id": event.execution_id,
            "correlation_id": event.correlation_id or "",
            "retry_count": retry_count,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if retry_count < self.max_retries:
            retry_event = WorkflowEventEnvelope(
                stream=self.retry_queue.stream_name,
                event_name=event.event_name,
                event_type=event.event_type,
                workflow_id=event.workflow_id,
                execution_id=event.execution_id,
                correlation_id=event.correlation_id,
                workflow_version=event.workflow_version,
                attempt=event.attempt,
                checkpoint_id=event.checkpoint_id,
                source=event.source,
                payload=event.payload,
                metadata={
                    **event.metadata,
                    "retry_count": retry_count + 1,
                    "retry_reason": str(exc),
                },
            )
            await self.retry_queue.enqueue(
                {
                    **retry_event.to_fields(),
                    "retry_count": str(retry_count + 1),
                    "retry_reason": str(exc),
                }
            )
            return

        await self.dead_letter_queue.enqueue(
            {
                **event.to_fields(),
                "dead_letter_reason": str(exc),
                "retry_count": str(retry_count),
                "failed_at": datetime.now(UTC).isoformat(),
                "failed_event": str(error_payload),
            }
        )
