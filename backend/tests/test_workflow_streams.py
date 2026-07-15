"""Workflow Redis Streams integration tests."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from redis.asyncio import Redis

from backend.events.workflow_streams import (
    DEFAULT_WORKFLOW_EVENT_STREAM,
    RedisWorkflowEventPublisher,
    WorkflowEventEnvelope,
)
from backend.queues.redis import RedisDeadLetterQueue, RedisRetryQueue, RedisStreamClient
from backend.workers.workflow_event_worker import WorkflowEventWorker
from backend.workflows.context import WorkflowContext


class FakeRedis:
    """Minimal async Redis double for stream integration tests."""

    def __init__(
        self,
        *,
        xreadgroup_response: list[tuple[str, list[tuple[str, dict[str, Any]]]]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.xreadgroup_response = xreadgroup_response or []
        self.counter = 0

    async def xadd(self, stream: str, fields: dict[str, Any], *, id: str) -> str:
        self.calls.append(("xadd", (stream, fields), {"id": id}))
        self.counter += 1
        return f"{self.counter}-0"

    async def xreadgroup(
        self,
        *,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, Any]]]]]:
        self.calls.append(
            (
                "xreadgroup",
                (groupname, consumername, streams),
                {"count": count, "block": block},
            )
        )
        return self.xreadgroup_response

    async def xgroup_create(
        self,
        *,
        name: str,
        groupname: str,
        id: str,
        mkstream: bool,
    ) -> None:
        self.calls.append(("xgroup_create", (name, groupname), {"id": id, "mkstream": mkstream}))

    async def xack(self, stream: str, group: str, *message_ids: str) -> int:
        self.calls.append(("xack", (stream, group, *message_ids), {}))
        return len(message_ids)


class FakeHandler:
    """Pluggable event handler double."""

    def __init__(
        self,
        *,
        fail_first: bool = False,
        always_fail: bool = False,
        block_event: asyncio.Event | None = None,
        started_event: asyncio.Event | None = None,
    ) -> None:
        self.events: list[WorkflowEventEnvelope] = []
        self.fail_first = fail_first
        self.always_fail = always_fail
        self.block_event = block_event
        self.started_event = started_event
        self.started = 0

    async def handle(self, event: WorkflowEventEnvelope) -> None:
        self.started += 1
        if self.started_event is not None:
            self.started_event.set()
        self.events.append(event)
        if self.block_event is not None:
            await self.block_event.wait()
        if self.always_fail or (self.fail_first and len(self.events) == 1):
            raise RuntimeError("transient failure")


def _connection(fake: FakeRedis) -> RedisStreamClient:
    from backend.infrastructure.redis import RedisConnection

    return RedisStreamClient(RedisConnection(cast(Redis, fake)))


def _context() -> WorkflowContext:
    return WorkflowContext(
        workflow_id="wf-1",
        workflow_version="1.0.0",
        execution_id="ex-1",
        correlation_id="corr-1",
        metadata={"source": "test"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "event_name", "event_type"),
    [
        ("publish_workflow_created", "workflow.created", "workflow.created.v1"),
        ("publish_workflow_started", "workflow.started", "workflow.started.v1"),
        ("publish_workflow_completed", "workflow.completed", "workflow.completed.v1"),
        ("publish_workflow_failed", "workflow.failed", "workflow.failed.v1"),
        ("publish_workflow_cancelled", "workflow.cancelled", "workflow.cancelled.v1"),
        ("publish_workflow_timed_out", "workflow.timed_out", "workflow.timed_out.v1"),
        ("publish_workflow_retried", "workflow.retried", "workflow.retried.v1"),
        ("publish_workflow_recovered", "workflow.recovered", "workflow.recovered.v1"),
        ("publish_workflow_checkpointed", "workflow.checkpointed", "workflow.checkpointed.v1"),
    ],
)
async def test_workflow_event_publisher_writes_envelope(
    method_name: str,
    event_name: str,
    event_type: str,
) -> None:
    fake = FakeRedis()
    streams = _connection(fake)
    publisher = RedisWorkflowEventPublisher(streams)
    context = _context()
    publish = getattr(publisher, method_name)

    envelope = await publish({"step": "planner"}, context)

    assert envelope.message_id == "1-0"
    assert envelope.stream == DEFAULT_WORKFLOW_EVENT_STREAM
    assert envelope.event_name == event_name
    assert envelope.event_type == event_type
    assert envelope.workflow_id == "wf-1"
    assert envelope.execution_id == "ex-1"
    assert envelope.correlation_id == "corr-1"
    assert fake.calls[0][0] == "xadd"
    stored_fields = fake.calls[0][1][1]
    assert stored_fields["event_name"] == event_name
    assert stored_fields["event_type"] == event_type
    assert stored_fields["workflow_id"] == "wf-1"
    assert stored_fields["execution_id"] == "ex-1"
    assert stored_fields["correlation_id"] == "corr-1"


@pytest.mark.asyncio
async def test_workflow_event_worker_retries_then_dlq() -> None:
    fake = FakeRedis()
    streams = _connection(fake)
    retry_queue = RedisRetryQueue(streams)
    dead_letter_queue = RedisDeadLetterQueue(streams)
    handler = FakeHandler(always_fail=True)
    worker = WorkflowEventWorker(
        streams=streams,
        handler=handler,
        retry_queue=retry_queue,
        dead_letter_queue=dead_letter_queue,
        stream_name="sentinel:workflow:events:v1",
        consumer_group="workflow-events",
        consumer_name="consumer-1",
        max_retries=1,
    )
    event = WorkflowEventEnvelope(
        stream="sentinel:workflow:events:v1",
        event_name="workflow.started",
        event_type="workflow.started.v1",
        workflow_id="wf-1",
        execution_id="ex-1",
        correlation_id="corr-1",
    )

    await worker._process_record(event.stream, "1-0", event.to_fields())

    retry_calls = [
        call
        for call in fake.calls
        if call[0] == "xadd" and call[1][0] == retry_queue.stream_name
    ]
    assert retry_calls
    retry_fields = retry_calls[0][1][1]
    retried_event = WorkflowEventEnvelope.from_fields(
        stream=retry_queue.stream_name,
        message_id="2-0",
        fields=retry_fields,
    )
    assert retried_event.metadata["retry_count"] == 1

    await worker._process_record(retry_queue.stream_name, "2-0", retry_fields)

    dlq_calls = [
        call
        for call in fake.calls
        if call[0] == "xadd" and call[1][0] == dead_letter_queue.stream_name
    ]
    assert dlq_calls
    assert any(call[0] == "xack" for call in fake.calls)


@pytest.mark.asyncio
async def test_workflow_event_worker_respects_backpressure() -> None:
    block = asyncio.Event()
    started = asyncio.Event()
    stop = asyncio.Event()
    handler = FakeHandler(block_event=block, started_event=started)
    fake = FakeRedis(
        xreadgroup_response=[
            (
                "sentinel:workflow:events:v1",
                [
                    (
                        "1-0",
                        WorkflowEventEnvelope(
                            stream="sentinel:workflow:events:v1",
                            event_name="workflow.started",
                            event_type="workflow.started.v1",
                            workflow_id="wf-1",
                            execution_id="ex-1",
                        ).to_fields(),
                    ),
                    (
                        "2-0",
                        WorkflowEventEnvelope(
                            stream="sentinel:workflow:events:v1",
                            event_name="workflow.completed",
                            event_type="workflow.completed.v1",
                            workflow_id="wf-1",
                            execution_id="ex-1",
                        ).to_fields(),
                    ),
                ],
            )
        ]
    )
    streams = _connection(fake)
    worker = WorkflowEventWorker(
        streams=streams,
        handler=handler,
        retry_queue=RedisRetryQueue(streams),
        dead_letter_queue=RedisDeadLetterQueue(streams),
        stream_name="sentinel:workflow:events:v1",
        consumer_group="workflow-events",
        consumer_name="consumer-1",
        max_in_flight=1,
        batch_size=2,
        block_ms=1,
        stop_event=stop,
    )

    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(started.wait(), timeout=1)
    assert handler.started == 1
    stop.set()
    block.set()
    await asyncio.wait_for(task, timeout=1)
    assert handler.started == 2
