# Recovery Engine Design

## Purpose

The Recovery Engine converts failure into explicit, audited workflow behavior. It classifies typed failures and selects retry, status verification, compensation, pause, or escalation without rewriting evidence or blindly repeating side effects.

## Decision Flow

```mermaid
flowchart TD
    F[Failure Event] --> C[Classify: transient, permanent, indeterminate, policy, cancellation]
    C --> S[Read current state, checkpoint, idempotency, deadline]
    S --> R{Safe retry eligible?}
    R -->|Yes| Q[Schedule bounded retry]
    R -->|No| V{External outcome known?}
    V -->|No| X[Query provider by idempotency key]
    V -->|Yes, compensable| P[Request policy-gated compensation]
    X --> P
    P --> E[Escalate or resume]
    Q --> E
```

## Controls

- Recovery begins from a durable failure event and current workflow state, never solely from a log line.
- Retries require transient classification, remaining deadline/budget, and idempotency safety; use exponential backoff and jitter.
- Indeterminate external side effects are verified by provider reference before any repeat or compensation.
- Recovery detects repeated fingerprints and stops loops; terminal escalation includes checkpoint, evidence, policy state, and audit trail.
- Every attempt emits `RecoveryStarted` and `RecoveryCompleted` and is evaluated for outcome and safety.

## Metrics

Track recovery success, time to recovery, retry exhaustion, compensation success, escalation rate, loop suppression, and unreconciled external outcomes. Recovery runbooks are exercised with chaos and restore tests.
