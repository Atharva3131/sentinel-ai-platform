# Security Design

## Security Objectives

The platform protects operational data, prevents unapproved side effects, and preserves an auditable account of every consequential decision. Security is based on least privilege, zero trust, defense in depth, and safe failure.

## Identity and Access

- Users authenticate through OIDC; services use managed/workload identities.
- API authorization combines scopes, roles, tenant/environment boundaries, and PolicyService decisions.
- Agents receive logical, short-lived capabilities only. They never receive database credentials or unrestricted shell/network access.
- High-risk changes and remediation require policy evaluation, and where configured, authenticated human approval.

## Data and Secrets

- Encrypt data in transit and at rest; use private endpoints for stateful services where available.
- Keep secrets in Azure Key Vault; do not put them in source, images, logs, events, prompts, or dashboards.
- Classify and minimize telemetry, prompts, logs, documents, and artifacts. Redact before model-provider or observability export.
- Use artifact references and checksums in events rather than copying raw evidence broadly.

## Runtime and Integration Controls

- Validate untrusted inputs, tool parameters, event schemas, and model outputs.
- Treat retrieved text as data, never executable instruction; isolate prompt-injection content from tool authority.
- Route all provider calls through typed adapters with allow-lists, timeout, rate-limit, circuit-breaker, and idempotency controls.
- Segment ingress, application, data, and egress networks; allow external egress only through approved adapters.

## Audit and Assurance

Every workflow transition, policy decision, approval, tool invocation, recovery action, configuration change, and privileged API call creates an append-oriented audit record with actor, timestamp, correlation, causation, and evidence references. Security testing includes dependency/image/secret scanning, authorization tests, threat modeling, penetration testing, and restore/replay verification.

## Incident Response

Security incidents first preserve evidence and restrict affected capabilities. Recovery uses revocation, credential rotation, policy/tool disablement, containment, audited remediation, and post-incident review. A loss of audit integrity or an unverified destructive tool outcome is treated as a high-severity event.
