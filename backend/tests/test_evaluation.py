"""Evaluation framework unit tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from backend.evaluation.context import EvaluationContext
from backend.evaluation.engine import EvaluationEngine
from backend.evaluation.exceptions import (
    EvaluationRegistryError,
)
from backend.evaluation.models import (
    EvaluationMetric,
    EvaluationMetricKind,
    EvaluationResult,
    EvaluationStatus,
    EvaluationSubject,
    EvaluationVerdict,
)
from backend.evaluation.pipeline import EvaluationPipeline
from backend.evaluation.registry import EvaluationRegistry

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeCancellationToken:
    _cancelled: bool = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_set(self) -> bool:
        return self._cancelled

    async def wait(self) -> None:
        while not self._cancelled:  # noqa: ASYNC110
            await asyncio.sleep(0)


@dataclass(slots=True)
class PassingStrategy:
    _name: str = "pass_strategy"
    _version: str | None = "1.0.0"
    _weight: float = 1.0
    _score: float = 1.0
    _confidence: float = 1.0

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str | None:
        return self._version

    @property
    def weight(self) -> float:
        return self._weight

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        now = datetime.now(UTC)
        metric = EvaluationMetric(
            name="score",
            kind=EvaluationMetricKind.CUSTOM,
            value=self._score,
            score=self._score,
            passed=True,
        )
        return EvaluationResult(
            strategy_name=self._name,
            strategy_version=self._version,
            status=EvaluationStatus.PASSED,
            metrics=(metric,),
            score=self._score,
            confidence=self._confidence,
            evidence_refs=(),
            started_at=now,
            ended_at=now,
        )


@dataclass(slots=True)
class FailingStrategy:
    _name: str = "fail_strategy"
    _version: str | None = "1.0.0"
    _weight: float = 1.0

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str | None:
        return self._version

    @property
    def weight(self) -> float:
        return self._weight

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        now = datetime.now(UTC)
        metric = EvaluationMetric(
            name="score",
            kind=EvaluationMetricKind.CUSTOM,
            value=0.0,
            score=0.0,
            passed=False,
        )
        return EvaluationResult(
            strategy_name=self._name,
            strategy_version=self._version,
            status=EvaluationStatus.FAILED,
            metrics=(metric,),
            score=0.0,
            confidence=1.0,
            evidence_refs=(),
            started_at=now,
            ended_at=now,
            reasoning="Threshold not met",
        )


@dataclass(slots=True)
class IndeterminateStrategy:
    @property
    def name(self) -> str:
        return "indet_strategy"

    @property
    def version(self) -> str | None:
        return "1.0.0"

    @property
    def weight(self) -> float:
        return 1.0

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        now = datetime.now(UTC)
        return EvaluationResult(
            strategy_name=self.name,
            strategy_version=self.version,
            status=EvaluationStatus.INDETERMINATE,
            metrics=(),
            score=0.5,
            confidence=0.4,
            evidence_refs=(),
            started_at=now,
            ended_at=now,
        )


@dataclass(slots=True)
class RaisingStrategy:
    @property
    def name(self) -> str:
        return "raising_strategy"

    @property
    def version(self) -> str | None:
        return "1.0.0"

    @property
    def weight(self) -> float:
        return 1.0

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        raise RuntimeError("unexpected internal error")


@dataclass(slots=True)
class BlockingStrategy:
    _sleep: float = 3600.0
    started: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def name(self) -> str:
        return "blocking_strategy"

    @property
    def version(self) -> str | None:
        return "1.0.0"

    @property
    def weight(self) -> float:
        return 1.0

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        self.started.set()
        await asyncio.sleep(self._sleep)
        now = datetime.now(UTC)
        return EvaluationResult(
            strategy_name=self.name,
            strategy_version=self.version,
            status=EvaluationStatus.PASSED,
            metrics=(),
            score=1.0,
            confidence=1.0,
            evidence_refs=(),
            started_at=now,
            ended_at=now,
        )


def _ctx(
    *,
    subject: EvaluationSubject = EvaluationSubject.WORKFLOW,
    evidence: dict | None = None,
    cancellation_token: FakeCancellationToken | None = None,
) -> EvaluationContext:
    return EvaluationContext(
        evaluation_id="eval-1",
        subject=subject,
        subject_id="wf-1",
        evidence=evidence or {},
        workflow_id="wf-1",
        cancellation_token=cancellation_token,
    )


def _registry(*strategies: tuple[str, object]) -> EvaluationRegistry:
    reg = EvaluationRegistry()
    for name, strategy in strategies:
        reg.register(
            name=name,
            version="1.0.0",
            provider=lambda _deps, s=strategy: s,  # type: ignore[arg-type]
            default=True,
        )
    return reg


# ---------------------------------------------------------------------------
# EvaluationMetric
# ---------------------------------------------------------------------------


def test_metric_valid_construction() -> None:
    m = EvaluationMetric(
        name="latency_p99",
        kind=EvaluationMetricKind.LATENCY,
        value=450.0,
        score=0.85,
        passed=True,
        unit="ms",
        threshold=500.0,
    )
    assert m.score == 0.85
    assert m.passed is True
    assert m.unit == "ms"


def test_metric_rejects_invalid_score() -> None:
    with pytest.raises(ValueError, match="score"):
        EvaluationMetric(
            name="bad",
            kind=EvaluationMetricKind.CUSTOM,
            value=1.0,
            score=1.5,
            passed=True,
        )


def test_metric_rejects_nonpositive_weight() -> None:
    with pytest.raises(ValueError, match="weight"):
        EvaluationMetric(
            name="bad",
            kind=EvaluationMetricKind.CUSTOM,
            value=1.0,
            score=0.5,
            passed=True,
            weight=0.0,
        )


def test_result_aggregate_score_weighted() -> None:
    m1 = EvaluationMetric(
        name="a", kind=EvaluationMetricKind.LATENCY, value=1.0, score=0.8, passed=True, weight=2.0
    )
    m2 = EvaluationMetric(
        name="b", kind=EvaluationMetricKind.COST, value=1.0, score=0.4, passed=False, weight=1.0
    )
    # (0.8*2 + 0.4*1) / 3 = 2.0/3 ≈ 0.667
    score = EvaluationResult.aggregate_score((m1, m2))
    assert abs(score - (2.0 / 3.0)) < 1e-9


def test_result_aggregate_score_empty() -> None:
    assert EvaluationResult.aggregate_score(()) == 0.0


# ---------------------------------------------------------------------------
# EvaluationContext
# ---------------------------------------------------------------------------


def test_context_not_cancelled_by_default() -> None:
    ctx = _ctx()
    assert not ctx.is_cancelled()


def test_context_cancelled_via_token() -> None:
    token = FakeCancellationToken()
    ctx = _ctx(cancellation_token=token)
    assert not ctx.is_cancelled()
    token.cancel()
    assert ctx.is_cancelled()


def test_context_has_timed_out_no_deadline() -> None:
    assert not _ctx().has_timed_out()


def test_context_remaining_seconds_no_deadline() -> None:
    assert _ctx().remaining_seconds() is None


def test_context_with_metadata() -> None:
    ctx = _ctx()
    ctx2 = ctx.with_metadata(run="test")
    assert ctx2.metadata["run"] == "test"
    assert "run" not in ctx.metadata


# ---------------------------------------------------------------------------
# EvaluationRegistry
# ---------------------------------------------------------------------------


def test_registry_registers_and_resolves() -> None:
    reg = EvaluationRegistry()
    strategy = PassingStrategy()
    reg.register(name="latency", version="1.0.0", provider=lambda _: strategy)
    ver, provider = reg.resolve("latency")
    assert ver == "1.0.0"
    assert provider(None) is strategy


def test_registry_highest_version_is_default() -> None:
    reg = EvaluationRegistry()
    s1 = PassingStrategy(_name="a")
    s2 = PassingStrategy(_name="a")
    reg.register(name="a", version="1.0.0", provider=lambda _: s1)
    reg.register(name="a", version="2.0.0", provider=lambda _: s2)
    ver, _ = reg.resolve("a")
    assert ver == "2.0.0"


def test_registry_explicit_version_override() -> None:
    reg = EvaluationRegistry()
    s1 = PassingStrategy(_name="a")
    s2 = PassingStrategy(_name="a")
    reg.register(name="a", version="1.0.0", provider=lambda _: s1)
    reg.register(name="a", version="2.0.0", provider=lambda _: s2)
    ver, provider = reg.resolve("a", "1.0.0")
    assert ver == "1.0.0"
    assert provider(None) is s1


def test_registry_raises_for_unknown_name() -> None:
    reg = EvaluationRegistry()
    with pytest.raises(EvaluationRegistryError, match="not registered"):
        reg.resolve("unknown")


def test_registry_raises_for_unknown_version() -> None:
    reg = EvaluationRegistry()
    reg.register(name="x", version="1.0.0", provider=lambda _: PassingStrategy())
    with pytest.raises(EvaluationRegistryError):
        reg.resolve("x", "99.0.0")


def test_registry_raises_on_duplicate_without_override() -> None:
    reg = EvaluationRegistry()
    reg.register(name="x", version="1.0.0", provider=lambda _: PassingStrategy())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(name="x", version="1.0.0", provider=lambda _: PassingStrategy())


def test_registry_override_replaces_existing() -> None:
    reg = EvaluationRegistry()
    s1 = PassingStrategy(_name="x")
    s2 = PassingStrategy(_name="x")
    reg.register(name="x", version="1.0.0", provider=lambda _: s1)
    reg.register(name="x", version="1.0.0", provider=lambda _: s2, override=True)
    _, provider = reg.resolve("x", "1.0.0")
    assert provider(None) is s2


def test_registry_names_and_versions() -> None:
    reg = EvaluationRegistry()
    reg.register(name="x", version="1.0.0", provider=lambda _: PassingStrategy())
    reg.register(name="x", version="2.0.0", provider=lambda _: PassingStrategy())
    assert reg.names() == ("x",)
    assert reg.versions("x") == ("1.0.0", "2.0.0")


def test_registry_create_returns_strategy_instance() -> None:
    reg = EvaluationRegistry()
    strategy = PassingStrategy()
    reg.register(name="s", version="1.0.0", provider=lambda _: strategy)
    result = reg.create("s")
    assert result is strategy


# ---------------------------------------------------------------------------
# EvaluationPipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_all_pass_gives_passed_verdict() -> None:
    pipeline = EvaluationPipeline(
        strategies=(PassingStrategy(_name="a"), PassingStrategy(_name="b"))
    )
    report = await pipeline.run(_ctx())
    assert report.verdict == EvaluationVerdict.PASSED
    assert report.overall_score == 1.0
    assert report.overall_confidence == 1.0
    assert report.passed_count() == 2


@pytest.mark.asyncio
async def test_pipeline_any_fail_gives_failed_verdict() -> None:
    pipeline = EvaluationPipeline(
        strategies=(PassingStrategy(), FailingStrategy())
    )
    report = await pipeline.run(_ctx())
    assert report.verdict == EvaluationVerdict.FAILED
    assert report.failed_count() == 1


@pytest.mark.asyncio
async def test_pipeline_indeterminate_verdict() -> None:
    pipeline = EvaluationPipeline(strategies=(IndeterminateStrategy(),))
    report = await pipeline.run(_ctx())
    assert report.verdict == EvaluationVerdict.INDETERMINATE


@pytest.mark.asyncio
async def test_pipeline_fail_fast_skips_remaining() -> None:
    pipeline = EvaluationPipeline(
        strategies=(FailingStrategy(), PassingStrategy(_name="b")),
        fail_fast=True,
    )
    report = await pipeline.run(_ctx())
    assert report.verdict == EvaluationVerdict.FAILED
    assert report.skipped_count() == 1
    assert len(report.results) == 2


@pytest.mark.asyncio
async def test_pipeline_strategy_error_produces_error_result() -> None:
    pipeline = EvaluationPipeline(strategies=(RaisingStrategy(),))
    report = await pipeline.run(_ctx())
    assert report.error_count() == 1
    assert report.verdict == EvaluationVerdict.INDETERMINATE
    assert report.results[0].status == EvaluationStatus.ERROR
    assert "unexpected internal error" in (report.results[0].reasoning or "")


@pytest.mark.asyncio
async def test_pipeline_timeout_produces_error_result() -> None:
    pipeline = EvaluationPipeline(
        strategies=(BlockingStrategy(),),
        timeout_seconds=0.01,
    )
    report = await pipeline.run(_ctx())
    assert report.error_count() == 1
    assert report.results[0].status == EvaluationStatus.ERROR
    assert report.results[0].error is not None


@pytest.mark.asyncio
async def test_pipeline_cancelled_skips_remaining() -> None:
    token = FakeCancellationToken()
    token.cancel()
    pipeline = EvaluationPipeline(
        strategies=(PassingStrategy(_name="a"), PassingStrategy(_name="b"))
    )
    report = await pipeline.run(_ctx(cancellation_token=token))
    assert report.skipped_count() == 2


@pytest.mark.asyncio
async def test_pipeline_empty_strategies_indeterminate() -> None:
    pipeline = EvaluationPipeline(strategies=())
    report = await pipeline.run(_ctx())
    assert report.verdict == EvaluationVerdict.INDETERMINATE
    assert report.overall_score == 0.0
    assert len(report.results) == 0


@pytest.mark.asyncio
async def test_pipeline_weighted_score() -> None:
    high = PassingStrategy(_name="high", _weight=3.0, _score=1.0)
    low = PassingStrategy(_name="low", _weight=1.0, _score=0.0)
    pipeline = EvaluationPipeline(strategies=(high, low))
    report = await pipeline.run(_ctx())
    # (1.0*3 + 0.0*1) / 4 = 0.75
    assert abs(report.overall_score - 0.75) < 1e-9


@pytest.mark.asyncio
async def test_pipeline_confidence_is_minimum() -> None:
    high_conf = PassingStrategy(_name="a", _confidence=0.9)
    low_conf = PassingStrategy(_name="b", _confidence=0.4)
    pipeline = EvaluationPipeline(strategies=(high_conf, low_conf))
    report = await pipeline.run(_ctx())
    assert report.overall_confidence == 0.4


@pytest.mark.asyncio
async def test_pipeline_report_populates_context_fields() -> None:
    pipeline = EvaluationPipeline(strategies=(PassingStrategy(),))
    ctx = EvaluationContext(
        evaluation_id="ev-99",
        subject=EvaluationSubject.AGENT,
        subject_id="agent-1",
        subject_version="2.0.0",
        evidence={},
        workflow_id="wf-99",
        execution_id="ex-99",
        correlation_id="corr-99",
    )
    report = await pipeline.run(ctx)
    assert report.evaluation_id == "ev-99"
    assert report.subject == EvaluationSubject.AGENT
    assert report.subject_id == "agent-1"
    assert report.subject_version == "2.0.0"
    assert report.workflow_id == "wf-99"
    assert report.execution_id == "ex-99"
    assert report.correlation_id == "corr-99"


# ---------------------------------------------------------------------------
# EvaluationReport helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_duration_seconds() -> None:
    pipeline = EvaluationPipeline(strategies=(PassingStrategy(),))
    report = await pipeline.run(_ctx())
    assert report.duration_seconds() >= 0.0


# ---------------------------------------------------------------------------
# EvaluationEngine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_resolves_and_runs() -> None:
    strategy = PassingStrategy()
    reg = EvaluationRegistry()
    reg.register(name="pass", version="1.0.0", provider=lambda _: strategy)
    engine = EvaluationEngine(registry=reg)
    report = await engine.evaluate(["pass"], _ctx())
    assert report.verdict == EvaluationVerdict.PASSED


@pytest.mark.asyncio
async def test_engine_raises_for_unregistered_strategy() -> None:
    engine = EvaluationEngine(registry=EvaluationRegistry())
    with pytest.raises(EvaluationRegistryError):
        await engine.evaluate(["missing"], _ctx())


@pytest.mark.asyncio
async def test_engine_respects_version_override() -> None:
    s1 = PassingStrategy(_name="s", _score=1.0)
    s2 = FailingStrategy(_name="s")
    reg = EvaluationRegistry()
    reg.register(name="s", version="1.0.0", provider=lambda _: s1)
    reg.register(name="s", version="2.0.0", provider=lambda _: s2)
    engine = EvaluationEngine(registry=reg)
    report = await engine.evaluate(["s"], _ctx(), versions={"s": "1.0.0"})
    assert report.verdict == EvaluationVerdict.PASSED


@pytest.mark.asyncio
async def test_engine_fail_fast_override() -> None:
    s_fail = FailingStrategy(_name="fail")
    s_pass = PassingStrategy(_name="pass")
    reg = EvaluationRegistry()
    reg.register(name="fail", version="1.0.0", provider=lambda _: s_fail)
    reg.register(name="pass", version="1.0.0", provider=lambda _: s_pass)
    engine = EvaluationEngine(registry=reg)
    report = await engine.evaluate(["fail", "pass"], _ctx(), fail_fast=True)
    assert report.skipped_count() == 1


@pytest.mark.asyncio
async def test_engine_timeout_override() -> None:
    blocking = BlockingStrategy()
    reg = EvaluationRegistry()
    reg.register(name="blocking", version="1.0.0", provider=lambda _: blocking)
    engine = EvaluationEngine(registry=reg)
    report = await engine.evaluate(
        ["blocking"], _ctx(), timeout_seconds=0.01
    )
    assert report.error_count() == 1


@pytest.mark.asyncio
async def test_engine_default_fail_fast_applies() -> None:
    s_fail = FailingStrategy(_name="fail")
    s_pass = PassingStrategy(_name="pass")
    reg = EvaluationRegistry()
    reg.register(name="fail", version="1.0.0", provider=lambda _: s_fail)
    reg.register(name="pass", version="1.0.0", provider=lambda _: s_pass)
    engine = EvaluationEngine(registry=reg, default_fail_fast=True)
    report = await engine.evaluate(["fail", "pass"], _ctx())
    assert report.skipped_count() == 1


@pytest.mark.asyncio
async def test_engine_multi_strategy_mixed() -> None:
    reg = EvaluationRegistry()
    reg.register(name="a", version="1.0.0", provider=lambda _: PassingStrategy(_name="a"))
    reg.register(name="b", version="1.0.0", provider=lambda _: PassingStrategy(_name="b"))
    reg.register(name="c", version="1.0.0", provider=lambda _: FailingStrategy(_name="c"))
    engine = EvaluationEngine(registry=reg)
    report = await engine.evaluate(["a", "b", "c"], _ctx())
    assert report.verdict == EvaluationVerdict.FAILED
    assert report.passed_count() == 2
    assert report.failed_count() == 1


# ---------------------------------------------------------------------------
# Metric kind coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        EvaluationMetricKind.LATENCY,
        EvaluationMetricKind.COST,
        EvaluationMetricKind.TOOL_SUCCESS,
        EvaluationMetricKind.WORKFLOW_SUCCESS,
        EvaluationMetricKind.AGENT_SUCCESS,
        EvaluationMetricKind.MEMORY_USAGE,
        EvaluationMetricKind.TOKEN_USAGE,
        EvaluationMetricKind.GROUNDEDNESS,
        EvaluationMetricKind.HALLUCINATION,
        EvaluationMetricKind.CUSTOM,
    ],
)
def test_all_metric_kinds_constructable(kind: EvaluationMetricKind) -> None:
    m = EvaluationMetric(name="m", kind=kind, value=1.0, score=1.0, passed=True)
    assert m.kind == kind
