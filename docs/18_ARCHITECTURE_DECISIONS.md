# Architecture Decisions

This document records the baseline architectural decisions. Each subsequent material change should add a dated ADR with context, decision, consequences, alternatives, and migration/rollback plan.

| ID | Decision | Rationale | Consequence |
| --- | --- | --- | --- |
| ADR-001 | Use event-driven workflow coordination. | Long-running autonomous work must survive process boundaries and scale independently. | Consumers must be idempotent and observable. |
| ADR-002 | Use PostgreSQL transactional state plus outbox. | Workflow transition and event publication need a reliable commit boundary. | External effects are reconciled, not part of distributed transactions. |
| ADR-003 | Use at-least-once delivery with effectively-once business behavior. | Distributed exactly-once claims are unsafe across streams and external tools. | Idempotency, deduplication, and recovery are mandatory. |
| ADR-004 | Keep `WorkflowRuntime` framework-neutral. | Business logic must not be coupled to LangGraph. | LangGraph is an adapter; test and future runtimes are possible. |
| ADR-005 | Use polyglot persistence. | Transactional state, streams/cache, graph traversal, documents, and artifacts have different access patterns. | Store ownership and cross-store reference rules must be maintained. |
| ADR-006 | Policy and approval gate side effects. | Model output is not authorization. | Executor rechecks policy and approval at the action boundary. |
| ADR-007 | Use GraphRAG with bounded traversal and provenance. | Incident diagnosis needs relationships and trusted evidence. | Agents cannot execute arbitrary graph queries. |
| ADR-008 | Make audit and observability first-class. | Autonomous operations need explanation, support, and compliance evidence. | Correlation and redaction are mandatory across modules. |
| ADR-009 | Deploy stateless containers around managed stateful services. | Enables independent scaling, restart safety, and cloud portability. | Durable state/checkpoints must be complete and tested. |
| ADR-010 | Start on managed Azure containers; preserve Kubernetes portability. | Reduces initial control-plane burden without foreclosing future scale. | Images, probes, configuration, and workers remain orchestration-neutral. |
| ADR-011 | Keep LLM, embedding, retrieval, and tool execution behind internal provider ports. | Model and tool frameworks should be swappable without leaking into services or workflows. | LangChain and provider SDKs become edge adapters; internal code depends on stable contracts only. |

## ADR Template

Each future ADR contains: status; context/problem; decision; alternatives; consequences; security/reliability/cost impact; migration; rollback; testing/observability requirements; and links to affected documents.
