"""Framework agent unit tests — Planner, Supervisor, Coordinator, HealthMonitor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.agents.context import AgentContext
from backend.agents.coordinator import CoordinatorAgent
from backend.agents.health_monitor import HealthMonitorAgent
from backend.agents.models import AgentInput
from backend.agents.planner import PlannerAgent
from backend.agents.supervisor import SupervisorAgent


def _ctx() -> AgentContext:
    return AgentContext(workflow_id="wf-1", execution_id="ex-1")


# ---------------------------------------------------------------------------
# PlannerAgent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_empty_tasks() -> None:
    agent = PlannerAgent()
    out = await agent.execute(_ctx(), AgentInput(payload={"tasks": []}))
    assert out.payload["plan"] == []
    assert out.payload["stages"] == []
    assert out.payload["cycle_detected"] is False


@pytest.mark.asyncio
async def test_planner_linear_chain() -> None:
    agent = PlannerAgent()
    tasks = [
        {"id": "a", "depends_on": []},
        {"id": "b", "depends_on": ["a"]},
        {"id": "c", "depends_on": ["b"]},
    ]
    out = await agent.execute(_ctx(), AgentInput(payload={"tasks": tasks}))
    assert out.payload["cycle_detected"] is False
    assert out.payload["plan"] == ["a", "b", "c"]
    assert out.payload["stages"] == [["a"], ["b"], ["c"]]


@pytest.mark.asyncio
async def test_planner_parallel_tasks() -> None:
    agent = PlannerAgent()
    tasks = [
        {"id": "root", "depends_on": []},
        {"id": "left", "depends_on": ["root"]},
        {"id": "right", "depends_on": ["root"]},
        {"id": "merge", "depends_on": ["left", "right"]},
    ]
    out = await agent.execute(_ctx(), AgentInput(payload={"tasks": tasks}))
    assert not out.payload["cycle_detected"]
    plan = out.payload["plan"]
    stages = out.payload["stages"]
    assert plan[0] == "root"
    assert plan[-1] == "merge"
    parallel_stage = stages[1]
    assert set(parallel_stage) == {"left", "right"}


@pytest.mark.asyncio
async def test_planner_detects_cycle() -> None:
    agent = PlannerAgent()
    tasks = [
        {"id": "a", "depends_on": ["b"]},
        {"id": "b", "depends_on": ["a"]},
    ]
    out = await agent.execute(_ctx(), AgentInput(payload={"tasks": tasks}))
    assert out.payload["cycle_detected"] is True
    assert out.payload["plan"] == []


@pytest.mark.asyncio
async def test_planner_ignores_unknown_dependencies() -> None:
    agent = PlannerAgent()
    tasks = [
        {"id": "a", "depends_on": ["external_unknown"]},
    ]
    out = await agent.execute(_ctx(), AgentInput(payload={"tasks": tasks}))
    assert not out.payload["cycle_detected"]
    assert out.payload["plan"] == ["a"]


@pytest.mark.asyncio
async def test_planner_single_task() -> None:
    agent = PlannerAgent()
    out = await agent.execute(
        _ctx(), AgentInput(payload={"tasks": [{"id": "solo", "depends_on": []}]})
    )
    assert out.payload["plan"] == ["solo"]
    assert out.payload["stages"] == [["solo"]]


# ---------------------------------------------------------------------------
# SupervisorAgent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supervisor_ok_when_no_issues() -> None:
    agent = SupervisorAgent()
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "workflow_id": "wf-99",
            "deadline": future,
            "last_activity_at": datetime.now(UTC).isoformat(),
            "budget_used": 10.0,
            "budget_limit": 100.0,
            "state_visit_counts": {"state_a": 2},
        }),
    )
    assert out.payload["verdict"] == "ok"
    assert out.payload["workflow_id"] == "wf-99"


@pytest.mark.asyncio
async def test_supervisor_deadline_exceeded() -> None:
    agent = SupervisorAgent()
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={"workflow_id": "wf-1", "deadline": past}),
    )
    assert out.payload["verdict"] == "deadline_exceeded"


@pytest.mark.asyncio
async def test_supervisor_stall_detected() -> None:
    agent = SupervisorAgent()
    old_activity = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "workflow_id": "wf-1",
            "last_activity_at": old_activity,
            "stall_threshold_seconds": 300,
        }),
    )
    assert out.payload["verdict"] == "stall_detected"


@pytest.mark.asyncio
async def test_supervisor_loop_detected() -> None:
    agent = SupervisorAgent()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "workflow_id": "wf-1",
            "state_visit_counts": {"processing": 15},
            "loop_threshold": 10,
        }),
    )
    assert out.payload["verdict"] == "loop_detected"
    assert "processing" in out.payload["reason"]


@pytest.mark.asyncio
async def test_supervisor_budget_exceeded() -> None:
    agent = SupervisorAgent()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "workflow_id": "wf-1",
            "budget_used": 100.0,
            "budget_limit": 50.0,
        }),
    )
    assert out.payload["verdict"] == "budget_exceeded"


@pytest.mark.asyncio
async def test_supervisor_deadline_checked_before_stall() -> None:
    agent = SupervisorAgent()
    past_deadline = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    old_activity = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "workflow_id": "wf-1",
            "deadline": past_deadline,
            "last_activity_at": old_activity,
            "stall_threshold_seconds": 300,
        }),
    )
    assert out.payload["verdict"] == "deadline_exceeded"


@pytest.mark.asyncio
async def test_supervisor_no_payload() -> None:
    agent = SupervisorAgent()
    out = await agent.execute(_ctx(), AgentInput(payload={}))
    assert out.payload["verdict"] == "ok"


# ---------------------------------------------------------------------------
# CoordinatorAgent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_all_ready_when_no_deps() -> None:
    agent = CoordinatorAgent()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "agents": [
                {"id": "a", "depends_on": []},
                {"id": "b", "depends_on": []},
            ],
            "completed": [],
        }),
    )
    assert set(out.payload["ready"]) == {"a", "b"}
    assert out.payload["blocked"] == []


@pytest.mark.asyncio
async def test_coordinator_blocked_when_deps_unmet() -> None:
    agent = CoordinatorAgent()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "agents": [
                {"id": "a", "depends_on": []},
                {"id": "b", "depends_on": ["a"]},
            ],
            "completed": [],
        }),
    )
    assert out.payload["ready"] == ["a"]
    assert out.payload["blocked"] == ["b"]
    assert out.payload["pending_deps"]["b"] == ["a"]


@pytest.mark.asyncio
async def test_coordinator_ready_after_dep_completes() -> None:
    agent = CoordinatorAgent()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "agents": [
                {"id": "a", "depends_on": []},
                {"id": "b", "depends_on": ["a"]},
            ],
            "completed": ["a"],
        }),
    )
    assert "b" in out.payload["ready"]
    assert out.payload["blocked"] == []


@pytest.mark.asyncio
async def test_coordinator_skips_already_completed() -> None:
    agent = CoordinatorAgent()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "agents": [
                {"id": "a", "depends_on": []},
                {"id": "b", "depends_on": ["a"]},
            ],
            "completed": ["a", "b"],
        }),
    )
    assert out.payload["ready"] == []
    assert out.payload["blocked"] == []


@pytest.mark.asyncio
async def test_coordinator_empty_agents() -> None:
    agent = CoordinatorAgent()
    out = await agent.execute(_ctx(), AgentInput(payload={"agents": [], "completed": []}))
    assert out.payload["ready"] == []
    assert out.payload["blocked"] == []


# ---------------------------------------------------------------------------
# HealthMonitorAgent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_monitor_healthy_no_errors() -> None:
    agent = HealthMonitorAgent()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "observations": [
                {"component": "api", "error_count": 0, "total_count": 100, "weight": 1.0},
                {"component": "db", "error_count": 1, "total_count": 1000, "weight": 1.0},
            ]
        }),
    )
    assert out.payload["status"] == "healthy"
    assert out.payload["weighted_error_fraction"] < 0.1


@pytest.mark.asyncio
async def test_health_monitor_degraded() -> None:
    agent = HealthMonitorAgent()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "observations": [
                {"component": "api", "error_count": 20, "total_count": 100, "weight": 1.0},
            ]
        }),
    )
    assert out.payload["status"] == "degraded"
    assert out.payload["component_statuses"]["api"] == "degraded"


@pytest.mark.asyncio
async def test_health_monitor_unhealthy() -> None:
    agent = HealthMonitorAgent()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "observations": [
                {"component": "api", "error_count": 60, "total_count": 100, "weight": 1.0},
            ]
        }),
    )
    assert out.payload["status"] == "unhealthy"
    assert out.payload["component_statuses"]["api"] == "unhealthy"


@pytest.mark.asyncio
async def test_health_monitor_no_observations() -> None:
    agent = HealthMonitorAgent()
    out = await agent.execute(_ctx(), AgentInput(payload={"observations": []}))
    assert out.payload["status"] == "healthy"
    assert out.payload["weighted_error_fraction"] == 0.0


@pytest.mark.asyncio
async def test_health_monitor_weighted_aggregation() -> None:
    agent = HealthMonitorAgent()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "observations": [
                # heavy component with low errors
                {"component": "db", "error_count": 5, "total_count": 100, "weight": 10.0},
                # light component with high errors
                {"component": "cache", "error_count": 80, "total_count": 100, "weight": 1.0},
            ]
        }),
    )
    # weighted fraction = (0.05*10 + 0.80*1) / 11 = 1.30/11 ≈ 0.118 → degraded
    assert out.payload["status"] == "degraded"


@pytest.mark.asyncio
async def test_health_monitor_zero_total_count_is_healthy() -> None:
    agent = HealthMonitorAgent()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "observations": [
                {"component": "idle", "error_count": 0, "total_count": 0, "weight": 1.0},
            ]
        }),
    )
    assert out.payload["status"] == "healthy"
    assert out.payload["component_statuses"]["idle"] == "healthy"


@pytest.mark.asyncio
async def test_health_monitor_custom_thresholds() -> None:
    agent = HealthMonitorAgent()
    out = await agent.execute(
        _ctx(),
        AgentInput(payload={
            "observations": [
                {"component": "svc", "error_count": 30, "total_count": 100, "weight": 1.0},
            ],
            "degraded_threshold": 0.5,
            "unhealthy_threshold": 0.9,
        }),
    )
    # 0.30 < degraded_threshold 0.50 → healthy with custom thresholds
    assert out.payload["status"] == "healthy"


@pytest.mark.asyncio
async def test_health_monitor_metadata() -> None:
    agent = HealthMonitorAgent()
    assert agent.metadata.name == "health_monitor"
    assert "health" in agent.metadata.capabilities
