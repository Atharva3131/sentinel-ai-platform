# Evaluation Design

## Purpose

Evaluation measures whether workflows and agents produce safe, grounded, useful outcomes. It is independent from the actor being evaluated and uses deterministic evidence checks before bounded model-assisted assessment.

## Evaluation Scope

| Subject | Measures |
| --- | --- |
| Retrieval and GraphRAG | Relevance, source authority, freshness, provenance, topology correctness. |
| Planner | Evidence grounding, hypothesis quality, plan completeness, risk awareness, action ordering. |
| Executor | Policy adherence, tool verification, idempotency, action effectiveness. |
| Workflow | Completion, timeliness, recovery, audit completeness, final service health. |
| Model/provider | Latency, token/cost use, structured-output validity, evaluator drift. |

## Evaluation Lifecycle

1. Select a versioned evaluation definition and evidence set.
2. Collect workflow, policy, tool, telemetry, retrieval, and outcome references.
3. Run deterministic rules, then configured assisted scoring where necessary.
4. Produce a report with score components, confidence, provenance, and pass/fail/indeterminate result.
5. Persist the report, emit `EvaluationCompleted`, and route failure or uncertainty to recovery/review.

## Dataset and Quality Controls

Evaluation datasets use sanitized, permissioned incident cases with expected evidence and outcomes. Definitions, prompts, scorers, and thresholds are versioned. Human review samples false-success risk, evaluator disagreement, and high-impact outcomes. A model or agent version is not promoted solely because it is faster or cheaper; it must meet grounding, safety, and outcome gates.

## Metrics

Track pass/fail/indeterminate rate, evidence coverage, agreement with operator outcomes, false-success/false-failure rate, recovery rate, latency, cost, and score drift. The Evaluation Dashboard compares results by workflow type, agent version, model, tool, and deployment revision.
