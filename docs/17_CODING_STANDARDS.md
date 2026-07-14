# Coding Standards

## Principles

The backend is Python 3.12, async-first, typed, modular, and production-oriented. Prefer clear composition over inheritance; make side effects explicit; keep services focused; and preserve the module dependency rules in the Low-Level Design.

## Required Practices

- Use type hints and typed validation schemas at external/module boundaries.
- Keep API handlers thin: validate transport data, establish context, call a service, and convert the response.
- Use constructor dependency injection and protocols/interfaces; no global mutable service state.
- Use async I/O APIs; isolate blocking/CPU-heavy work in bounded execution paths.
- Emit structured, correlated telemetry and typed exceptions. Never log secrets or raw sensitive payloads.
- Make event consumers, workflow stages, and external tool calls idempotent; define timeout, retry, cancellation, and error classification.
- Put business use cases in services, persistence in repositories, engine details in runtime adapters, and policy decisions in policy components.
- Add tests proportionate to change risk and update versioned contracts/documentation when behavior changes.

## Prohibited Practices

- No direct database access from agents or API routes.
- No direct LangGraph dependency outside the runtime adapter.
- No policy component calling APIs/tools, and no model output granting authority.
- No secrets in repository, image, log, event, test fixture, or documentation example.
- No unbounded background tasks, free-form external tool access, unbounded graph queries, or silent exception swallowing.

## Review Checklist

Reviewers verify module boundaries, schema compatibility, authorization/policy enforcement, idempotency, error/recovery behavior, observability, tests, security/redaction, migrations, and operational rollout/rollback implications.
