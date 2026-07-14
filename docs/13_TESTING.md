# Testing Strategy

## Test Pyramid

| Level | Scope | Primary purpose |
| --- | --- | --- |
| Unit | Models, services, policies, state transitions, retry classification, utilities. | Fast deterministic business-rule feedback. |
| Integration | Repositories, migrations, Redis Streams, outbox, graph, cache, object storage adapters. | Validate real protocols and durability behavior. |
| Contract | API, event, runtime, tool, webhook, and provider interfaces. | Prevent independently deployed components drifting. |
| Workflow | End-to-end happy, approval, cancellation, duplicate, recovery, and escalation paths. | Validate cross-service lifecycle behavior. |
| Load/soak | Alert storms, queue lag, concurrent workflows, model/tool saturation. | Validate scale, latency, and backpressure. |
| Chaos/DR | Worker loss, dependency outage, duplicate/delayed events, restore and failover. | Validate recovery and evidence preservation. |

## Test Principles

Tests are deterministic where possible, use fake runtime/tool/model adapters for unit and workflow tests, and use disposable real dependencies for integration tests. Production data is never copied into tests without explicit sanitization and approval. Every release validates authorization, policy gating, audit completeness, idempotency, trace propagation, schema compatibility, and secret redaction.

## Release Gates

CI requires unit, contract, migration, and security tests. Staging promotion additionally requires synthetic read-only workflow, telemetry/dashboard checks, and event/outbox verification. Production release requires load, recovery, restore/reconciliation, and release-candidate evaluation evidence appropriate to the changed risk surface.
