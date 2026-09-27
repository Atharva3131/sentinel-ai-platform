"""Test/bootstrap helpers for wiring an EvaluationRegistry for SRE confidence."""

from __future__ import annotations

from backend.evaluation.registry import EvaluationRegistry
from backend.retrieval.retrieval_evaluator import register_retrieval_strategies


def _make_evaluation_registry(
    registry: EvaluationRegistry | None = None,
    *,
    override: bool = False,
) -> EvaluationRegistry:
    """Return a registry with the SRE retrieval evaluation strategies registered.

    Used during tests and application bootstrap.
    """
    reg = registry or EvaluationRegistry()
    register_retrieval_strategies(reg, override=override)
    return reg
