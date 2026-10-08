# PROJECT_CONTEXT.md - Sentinel AI Platform

**Generated:** October 2026  
**Project:** sentinel-ai-platform v0.1.0  
**Status:** Production-Ready (Core Loop) / Beta (Extended Features)

---

## 1. Project Overview

### Project Name
**Sentinel AI Platform** — A production-grade autonomous AI operations runtime.

### Description
Sentinel AI is an end-to-end incident response automation platform that closes the operational loop. It ingests incidents (alerts, metrics anomalies, deployment failures), automatically investigates root causes using AI/LLM agents, plans and executes remediations, deploys fixes, and verifies resolution—all without human intervention.

### Problem Being Solved
Modern cloud operations suffer from **alert fatigue** and **manual incident toil**. Teams spend hours investigating incidents, triaging evidence, planning fixes, and verifying resolutions. Sentinel eliminates this toil by automating the entire incident lifecycle using evidence collection, AI reasoning, policy-gated execution, and closed-loop verification.

### Target Users
- **SRE/DevOps teams** managing complex cloud infrastructure
- **Platform engineering teams** building internal control planes
- **Enterprises** seeking to reduce MTTR and operational toil
- **AIOps platforms** seeking autonomous incident handling capabilities

### Core Use Case
**Incident: High CPU on service-auth**
```
Incoming Alert (Prometheus)
    ↓
Automatically collect evidence (CPU metrics, logs, traces, recent deployments, traffic patterns)
    ↓
Run LLM investigation agent: "Given this evidence, what caused the CPU spike?"
    ↓
Generate RCA with confidence score (e.g., "Inefficient cache invalidation after recent rollout")
    ↓
Execute remediation (e.g., "Roll back latest deployment", "Scale up service")
    ↓
Deploy fix via GitHub Actions
    ↓
Verify resolution (CPU returns to baseline, latency recovers, error rate drops)
    ↓
Incident marked RESOLVED
    ↓
Human team reviews automated analysis asynchronously
```

### Current Project Maturity
- **Investigation + Remediation loops:** Fully implemented and tested (800 tests passing)
- **Deployment verification:** Production-ready
- **LLM integration:** Multiple providers (Mistral, Sarvam, OpenAI-compatible)
- **Evidence collection:** Prometheus (metrics), Elastic (logs), OTLP (traces), knowledge bases
- **Feature flags:** GraphRAG disabled, evaluation pipeline incomplete, scheduler dormant

### Current Implementation Status
✅ **Core closed-loop orchestration:** Investigation → Remediation → Deployment → Verification  
✅ **PostgreSQL incident store:** Full CRUD with audit trail  
✅ **Azure infrastructure:** KeyVault, Managed Identity, Application Insights  
✅ **Redis streams:** Event-driven architecture  
✅ **LLM investigation agents:** Structured outputs with tool use  
✅ **GitHub Actions integration:** PR-based remediation deployment  
⚠️ **Graph RAG:** Implemented but disabled (Neo4j optional, feature flag off)  
❌ **Approval workflows:** HIGH/CRITICAL actions require approval (gate logic exists, tracking not implemented)  
❌ **Notification delivery:** Feature flag present, implementation incomplete  
❌ **Frontend:** Placeholder only  

### Key Differentiators
1. **Incident-type agnostic:** Does not hard-code failure modes; works for any incident with extractable evidence
2. **Bounded loops with graceful degradation:** Reinvestigates up to 3 cycles; partial evidence acceptable
3. **Policy-gated remediation:** Risk levels (LOW/MEDIUM/HIGH/CRITICAL) with approval gates
4. **Evidence-driven reasoning:** LLM agents grounded in actual collected evidence
5. **Idempotency-first:** All actions replayable; deployment requests deduplicated per cycle

### Overall Technology Stack
- **Runtime:** FastAPI + Uvicorn (async-first)
- **Language:** Python 3.12+
- **Database:** PostgreSQL 17 (primary) + Redis 7.4 (cache/streams) + Neo4j 5 (optional)
- **LLM Providers:** OpenAI-compatible (Mistral, Sarvam, Azure OpenAI)
- **Cloud:** Azure (KeyVault, Managed Identity, Application Insights, Cosmos, Blob Storage)
- **CI/CD:** GitHub Actions + Azure Container Apps
- **Observability:** OpenTelemetry + Azure Monitor

---

## 2. Repository Structure

```
sentinel-ai-platform/
├── backend/                            # Python backend application
│   ├── application/                    # FastAPI app factory, DI container, providers
│   │   ├── factory.py                  # create_application() entry point
│   │   ├── container.py                # ApplicationContainer: resource lifecycle
│   │   ├── providers.py                # Dishka DI provider definitions
│   │   └── health.py                   # Health check orchestration
│   ├── api/                            # HTTP API routes and schemas
│   │   ├── router.py                   # build_api_router() composition
│   │   ├── routers/                    # Individual route modules (health, incidents, metrics)
│   │   └── schemas/                    # Pydantic request/response models
│   ├── services/                       # Business logic orchestrators
│   │   ├── closed_loop_orchestrator.py # Main incident lifecycle driver
│   │   ├── investigation_orchestrator.py # Evidence collection → RCA
│   │   ├── remediation_engine.py       # Policy-gated remediation execution
│   │   ├── deployment_pipeline.py      # GitHub Actions integration
│   │   ├── verification_coordinator.py # Post-deployment health checks
│   │   ├── evidence_orchestrator.py    # Multi-provider evidence collection
│   │   ├── incident_ingestion.py       # Incident validation and persistence
│   │   └── incident_repository.py      # Incident data access layer
│   ├── core/                           # Domain logic (not I/O bound)
│   │   ├── hypothesis_engine.py        # Root cause generation
│   │   ├── remediation_engine.py       # (duplicate name?) Remediation action execution
│   │   ├── validation_engine.py        # Pre-deployment validation
│   │   ├── evidence_collector.py       # Evidence aggregation logic
│   │   └── ...
│   ├── models/                         # Domain dataclasses (immutable)
│   │   ├── incident.py                 # Incident, IncidentSignal, IncidentStatus
│   │   ├── evidence.py                 # Evidence, EvidenceSource, EvidenceSourceKind
│   │   ├── hypothesis.py               # Hypothesis, RootCauseAnalysis
│   │   ├── remediation.py              # RemediationAction, ActionRiskLevel
│   │   ├── deployment.py               # DeploymentRequest, DeploymentStatus
│   │   ├── resolution.py               # IncidentResolution, ResolutionStatus
│   │   ├── validation.py               # ValidationStrategyConfig
│   │   └── ...
│   ├── db/                             # Data persistence layer
│   │   ├── models/                     # ORM models (SQLAlchemy)
│   │   │   └── incident.py             # IncidentORM (incidents table)
│   │   ├── repositories/               # Data access objects
│   │   │   ├── incident.py             # PostgreSQLIncidentRepository
│   │   │   └── base.py                 # BaseRepository mixin
│   │   ├── migrations/                 # Alembic schema migrations
│   │   ├── session.py                  # DatabaseSessionManager, async session factory
│   │   ├── engine.py                   # create_postgres_engine()
│   │   ├── health.py                   # PostgresHealthCheck
│   │   ├── retry.py                    # RetryPolicy for transient failures
│   │   └── base.py                     # ORM base class
│   ├── providers/                      # External system adapters (factories)
│   │   ├── llm/                        # LLM provider adapters
│   │   │   ├── factory.py              # build_llm_provider() for Mistral, Sarvam, OpenAI
│   │   │   ├── openai_compat.py        # OpenAICompatibleLLMAdapter
│   │   │   ├── sarvam.py               # SarvamLLMAdapter
│   │   │   ├── mistral.py              # MistralLLMAdapter
│   │   │   ├── fallback.py             # FallbackLLMProvider (primary → fallback)
│   │   │   └── ...
│   │   ├── evidence/                   # Evidence source adapters
│   │   │   ├── factory.py              # build_metrics_provider() etc.
│   │   │   ├── metrics.py              # PrometheusMetricsProvider
│   │   │   ├── azure_monitor.py        # AzureMonitorMetricsProvider
│   │   │   ├── logs.py                 # ElasticLogsProvider
│   │   │   ├── traces.py               # OTLPTracesProvider
│   │   │   └── base.py                 # EvidenceHttpBase (shared HTTP client)
│   │   ├── github/                     # GitHub integration
│   │   │   ├── client.py               # GitHubClient (REST API wrapper)
│   │   │   ├── deployment.py           # GitHubActionsDeploymentProvider
│   │   │   ├── executor.py             # GitHubActionExecutor (workflow trigger)
│   │   │   ├── planner.py              # GitHubRemediationPlanner
│   │   │   └── deployment_factory.py   # build_deployment_provider()
│   │   ├── metrics/                    # Metrics snapshot (verification)
│   │   │   ├── azure_monitor_snapshot.py # AzureMonitorMetricsSnapshot (Application Insights)
│   │   │   └── ...
│   │   └── ...
│   ├── infrastructure/                 # Cloud/database clients and integrations
│   │   ├── redis.py                    # create_redis_client(), RedisConnection
│   │   ├── neo4j.py                    # create_neo4j_driver(), Neo4jConnection
│   │   ├── cosmos.py                   # create_cosmos_client(), CosmosConnection
│   │   ├── azure/                      # Azure SDK wrappers
│   │   │   ├── key_vault.py            # AzureKeyVault (secrets fetching)
│   │   │   └── ...
│   │   ├── health_checks.py            # Health probes for all backends
│   │   └── ...
│   ├── configuration/                  # Settings and environment configuration
│   │   └── settings.py                 # AppSettings, all configuration classes
│   ├── interfaces/                     # Protocol definitions (abstract contracts)
│   │   ├── llm.py                      # LLMProvider protocol
│   │   ├── evidence.py                 # MetricsEvidenceProvider protocol etc.
│   │   ├── deployment.py               # DeploymentProvider protocol
│   │   ├── health.py                   # HealthCheck protocol
│   │   ├── retrieval.py                # RetrievalProvider protocol
│   │   ├── fake_evidence.py            # FakeLLMProvider, FakeMetricsProvider
│   │   ├── fake_llm.py                 # Test doubles for LLM
│   │   └── ...
│   ├── policies/                       # Risk evaluation and policy gates
│   │   ├── action_policy.py            # RemediationPolicyEngine
│   │   └── ...
│   ├── events/                         # Event publishing and lifecycle events
│   │   ├── workflow_streams.py         # RedisWorkflowEventPublisher
│   │   └── incident_events.py          # Event type definitions
│   ├── queues/                         # Redis queue abstractions
│   │   ├── redis.py                    # RedisStreamClient, RedisPubSub, locks, retry queues
│   │   └── ...
│   ├── cache/                          # Caching layer
│   │   ├── redis.py                    # RedisCache wrapper
│   │   └── ...
│   ├── retrieval/                      # Knowledge retrieval (optional, RAG)
│   │   ├── pipeline.py                 # RetrievalPipeline
│   │   ├── graph.py                    # GraphRetriever (Neo4j)
│   │   └── ...
│   ├── runtime/                        # Runtime abstraction for actions
│   │   ├── __init__.py                 # RuntimeFactory, RuntimeRegistry
│   │   ├── middleware.py               # RuntimeMiddlewarePipeline
│   │   └── ...
│   ├── tools/                          # Evidence request tools for LLM
│   │   └── ...
│   ├── evaluation/                     # Evaluation framework (incomplete)
│   │   ├── engine.py                   # EvaluationEngine
│   │   ├── registry.py                 # EvaluationRegistry
│   │   ├── strategies/                 # Investigation strategy implementations
│   │   └── ...
│   ├── telemetry/                      # OpenTelemetry instrumentation
│   │   ├── __init__.py                 # TelemetryHandle setup
│   │   └── ...
│   ├── logging/                        # Structured logging setup
│   │   └── ...
│   ├── middleware/                     # FastAPI middleware
│   │   └── ...
│   ├── tests/                          # Unit/integration tests (800+ tests)
│   │   ├── test_configuration.py
│   │   ├── test_llm_investigation_agent.py
│   │   ├── test_rag_sre_knowledge.py
│   │   ├── test_redis.py
│   │   ├── test_key_vault_llm_integration.py
│   │   ├── conftest_db.py              # Fixtures
│   │   └── ... (40+ test files)
│   └── ...
├── frontend/                           # Frontend placeholder (not implemented)
│   └── .gitkeep
├── docker/                             # Docker build context
│   └── ...
├── infrastructure/                     # Infrastructure as code (placeholder)
│   └── .gitkeep
├── scripts/                            # Utility scripts
│   └── .gitkeep
├── docs/                               # Documentation
│   └── ...
├── .github/                            # GitHub workflows
│   └── workflows/
│       └── deploy-azure.yml            # CI/CD: test → lint → build → deploy to ACA
├── main.py                             # ASGI app entry point
├── pyproject.toml                      # Project metadata and dependencies
├── uv.lock                             # Locked dependency versions
├── alembic.ini                         # Alembic configuration
├── docker-compose.yml                  # Local dev stack (postgres, redis, neo4j)
├── Dockerfile                          # Production image
├── .env                                # Environment configuration (dev)
├── .env.example                        # Template env
├── CHANGELOG.md                        # Version history
├── LICENSE                             # License
├── CONTRIBUTING.md                     # Contributor guidelines
└── PROJECT_CONTEXT.md                  # This file
```

---

## 3. System Architecture

### High-Level Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          INCIDENT INGESTION                             │
│  POST /api/v1/incidents (Alert, Metric Threshold, Log Pattern, etc.)   │
│  IncidentIngestionService validates, deduplicates, persists to DB      │
└────────────────────────────┬────────────────────────────────────────────┘
                             ↓
┌────────────────────────────────────────────────────────────────────────┐
│                   CLOSED-LOOP ORCHESTRATOR (Entry Point)               │
│                                                                         │
│  for cycle in 1..3:                                                    │
│    ┌───────────────────────────────────────────────────────────────┐  │
│    │ INVESTIGATION PHASE                                           │  │
│    │  • EvidenceOrchestrator collects from parallel providers:    │  │
│    │    - MetricsEvidenceProvider (Prometheus / Azure Monitor)    │  │
│    │    - LogsEvidenceProvider (Elastic)                         │  │
│    │    - TracesEvidenceProvider (OTLP/Jaeger)                  │  │
│    │    - Custom: GitHub deployments, infrastructure config     │  │
│    │  • Graceful degradation if provider fails                  │  │
│    │  • Evidence normalized into EvidenceCollection             │  │
│    │  • LLMInvestigationAgent generates hypotheses              │  │
│    │  • DeterministicHypothesisGenerator or LLM fallback        │  │
│    │  • Return RootCauseAnalysis with confidence score          │  │
│    └────────────────┬────────────────────────────────────────────┘  │
│                     ↓                                                 │
│    ┌───────────────────────────────────────────────────────────────┐  │
│    │ REMEDIATION PHASE                                             │  │
│    │  • RemediationEngine evaluates risk level (policy gate)       │  │
│    │  • GitHubRemediationPlanner generates action plan            │  │
│    │  • Remediation actions: deploy, scale, rollback, restart     │  │
│    └────────────────┬────────────────────────────────────────────┘  │
│                     ↓                                                 │
│    ┌───────────────────────────────────────────────────────────────┐  │
│    │ DEPLOYMENT PHASE                                              │  │
│    │  • DeploymentPipeline triggers GitHub Actions workflow        │  │
│    │  • GitHubActionsDeploymentProvider posts to GitHub API        │  │
│    │  • Idempotency key: per-cycle deployment deduplication       │  │
│    └────────────────┬────────────────────────────────────────────┘  │
│                     ↓                                                 │
│    ┌───────────────────────────────────────────────────────────────┐  │
│    │ VERIFICATION PHASE                                            │  │
│    │  • VerificationCoordinator runs health checks                │  │
│    │  • VerificationEngine queries AzureMonitorMetricsSnapshot    │  │
│    │  • Metrics compared: error_rate, latency_ms, exception_rate  │  │
│    │  • Decision: PASS (resolve) | FAIL (reinvestigate)         │  │
│    └────────────────┬────────────────────────────────────────────┘  │
│                     ↓                                                 │
│              if PASS → RESOLVED                                       │
│              if FAIL and cycle < 3 → loop                            │
│              if FAIL and cycle >= 3 → ESCALATED                     │
└─────────────────────────────────────────────────────────────────────────┘
                             ↓
                    PostgreSQL Incident Store
                    Redis Event Stream (pub/sub)
                    Application Insights (tracing/metrics)
```

### Data Models

**Incident** (immutable dataclass):
- `incident_id`, `title`, `severity`, `status`, `affected_services`, `signals`, `symptoms`, `correlation_id`

**Evidence** (multiple types):
- `source_kind` (METRICS, LOGS, TRACES, DEPLOYMENT, INFRASTRUCTURE, KNOWLEDGE)
- `title`, `content`, `severity`, `relevance_score`, `structured_data`

**RootCauseAnalysis**:
- `hypothesis`, `confidence`, `supporting_evidence`, `reasoning`

**RemediationAction**:
- `action_type` (scale, rollback, deploy, restart), `risk_level` (LOW/MEDIUM/HIGH/CRITICAL), `status`

**VerificationOutcome**:
- `passed`, `failure_reason`, `metrics_before/after`, `deployment_id`

**IncidentResolution**:
- `resolution_id`, `status` (RESOLVED, ESCALATED, FAILED), `duration_ms`, `summary`

---

## 4. Technology Stack

| Layer | Technology | Version | Purpose |
|---|---|---|---|
| **Language** | Python | 3.12+ | Type-safe, async-first |
| **Web Framework** | FastAPI | 0.115+ | Modern ASGI HTTP server |
| **ASGI Server** | Uvicorn | 0.34+ | Async HTTP serving |
| **Validation** | Pydantic | 2.10+ | Data validation & serialization |
| **Configuration** | Pydantic Settings | 2.7+ | Environment-aware config with secrets |
| **DI Container** | Dishka | 1.10+ | Scoped dependency injection (APP, REQUEST) |
| **ORM** | SQLAlchemy | 2.0+ (asyncio) | Async-only relational ORM |
| **Database Driver** | asyncpg | 0.30+ | High-performance async PostgreSQL |
| **Schema Migrations** | Alembic | 1.14+ | VCS-driven database migrations |
| **Cache/Streams/Locks** | Redis | 7.4+ | Cache, event streams, distributed locks |
| **Redis Client** | redis-py | 5.2+ | Async Redis wrapper |
| **Redis Auth** | redis-entraid | 1.2.1+ | Azure Entra authentication |
| **Graph Database** | Neo4j | 5.27+ | Knowledge graph (optional) |
| **Document Store** | Azure Cosmos DB | 4.9+ | Document store (optional) |
| **Unstructured Storage** | Azure Blob Storage | 12.24+ | Object storage |
| **LLM Providers** | httpx + vendor SDKs | 0.28+ | Async HTTP for LLM APIs |
| **LLM Compatibility** | OpenAI API spec | — | Works with Mistral, Sarvam, Azure OpenAI |
| **Structured Output** | JSON Schema | — | LLM tool definitions and structured responses |
| **Observability** | OpenTelemetry | 1.29+ | Distributed tracing & metrics |
| **Traces Export** | OTLP HTTP | 1.29+ | OTLP protocol to observability backends |
| **Metrics Export** | Prometheus Client | 0.21+ | Prometheus-compatible metrics endpoint |
| **Azure Monitor Export** | Azure OpenTelemetry Exporter | 1.0.0b33+ | Azure Application Insights integration |
| **Instrumentation** | OTel FastAPI, SQLAlchemy, httpx, Redis | 0.50b0+ | Auto-instrumentation plugins |
| **Structured Logging** | structlog | 24.4+ | Structured JSON logging |
| **Azure SDK** | azure-identity | 1.19+ | Managed Identity & Service Principal auth |
| **Azure SDK** | azure-keyvault-secrets | 4.11.3+ | Secret management |
| **Azure SDK** | azure-cosmos | 4.9+ | Cosmos DB client |
| **Azure SDK** | azure-storage-blob | 12.24+ | Blob Storage client |
| **Testing Framework** | pytest | 8.3+ | Unit/integration testing |
| **Async Testing** | pytest-asyncio | 0.25+ | Async test support |
| **Type Checking** | mypy | 1.14+ | Static type analysis |
| **Linting** | Ruff | 0.9+ | Fast Python linter |
| **Package Manager** | uv | — | Ultra-fast Python package manager |
| **CI/CD** | GitHub Actions | — | Build, test, deploy workflow |
| **Deployment** | Azure Container Apps | — | Managed container orchestration |
| **Deployment Docker** | Docker | — | Container image building |
| **Container Registry** | Azure Container Registry | — | Private Docker image storage |

---

## 5. Backend

### Framework & Startup

**Framework:** FastAPI 0.115+  
**Language:** Python 3.12+ (type-safe, async-first)  
**Entry Point:** `main.py`

```python
from backend.application.factory import create_application
app = create_application()

# Uvicorn: uvicorn main:app --host 0.0.0.0 --port 8000
```

**Application Factory** (`backend/application/factory.py`):
1. Load `AppSettings` from environment
2. Build `ApplicationContainer` (async initialization of all resources)
3. Construct FastAPI app with all routers, middleware, exception handlers
4. Register lifecycle hooks (lifespan context manager for resource cleanup)

### API Routes

| Method | Endpoint | Purpose | Auth | Logic |
|---|---|---|---|---|
| `GET` | `/health` | Service identity & basic health (no I/O) | None | Returns app name, version, uptime |
| `GET` | `/health/live` | Liveness probe (can accept requests) | None | Always returns 200 OK |
| `GET` | `/health/ready` | Readiness probe (all backends ready) | None | Queries PostgreSQL, Redis, Neo4j (if enabled), Cosmos (if enabled) |
| `GET` | `/metrics` | Prometheus-compatible metrics | None | OpenTelemetry instrumentation export |
| `POST` | `/api/v1/incidents` | Ingest new incident | None | IncidentIngestionService → validate, deduplicate, persist |
| `GET` | `/api/v1/incidents/{incident_id}` | Retrieve single incident | None | PostgreSQLIncidentRepository.get() |
| `GET` | `/api/v1/incidents` | List incidents with filtering | None | Query: `status`, `severity`, `environment`, pagination |

**Request/Response Examples:**

```python
# POST /api/v1/incidents
CreateIncidentRequest(
    title="High CPU on service-auth",
    description="CPU above 90% for 5 minutes",
    severity=IncidentSeverity.HIGH,
    affected_services=["service-auth"],
    symptoms=["High CPU usage"],
    signals=[
        IncidentSignal(
            signal_id="alert-123",
            signal_type=SignalType.METRIC_THRESHOLD,
            source=SignalSource.PROMETHEUS,
            title="CPU threshold",
            description="...",
            severity=IncidentSeverity.HIGH,
            received_at=datetime.now(UTC),
        )
    ],
    correlation_id="corr-xyz",  # Optional, for deduplication
)

# Response: 201 Created
CreateIncidentResponse(
    incident_id="incident-abc123",
    is_duplicate=False,
    existing_incident_id=None,
    created_at=datetime.now(UTC),
)

# GET /api/v1/incidents/{incident_id}
Response: Incident(incident_id, title, status, affected_services, ...)

# GET /api/v1/incidents?status=investigating&severity=high&limit=20&offset=0
Response: ListIncidentsResponse(incidents=[...], total_count=42, has_more=True)
```

### Service Layer Architecture

| Service | Responsibility | Key Methods | Dependencies |
|---|---|---|---|
| **ClosedLoopOrchestrator** | Drives incident lifecycle (investigation → remediation → deployment → verification) | `run(incident)` → `ClosedLoopResult` | InvestigationOrchestrator, RemediationEngine, DeploymentPipeline, VerificationCoordinator, IncidentRepository |
| **InvestigationOrchestrator** | Iterative evidence collection → hypothesis generation → RCA | `investigate(incident, execution_id, context)` → `InvestigationResult` | EvidenceOrchestrator, HypothesisEngine, LLMInvestigationAgent |
| **EvidenceOrchestrator** | Parallel collection from multiple evidence providers | `collect(incident, source_kinds, max_concurrency)` → `EvidenceCollection` | MetricsEvidenceProvider, LogsEvidenceProvider, TracesEvidenceProvider |
| **RemediationEngine** | Policy-gated execution of remediation actions | `execute_plan(plan, incident, correlation_id)` | RemediationPolicyEngine, action executors |
| **DeploymentPipeline** | Triggers GitHub Actions workflows; async tracking | `deploy(request, incident, context)` → `DeploymentResult` | GitHubActionsDeploymentProvider |
| **VerificationCoordinator** | Post-deployment health checks against verification plan | `verify(incident, plan, deployment, execution_id)` → `VerificationOutcome` | VerificationEngine, AzureMonitorMetricsSnapshot |
| **IncidentIngestionService** | Validates, deduplicates, persists new incidents | `ingest(request)` → `CreateIncidentResponse` | PostgreSQLIncidentRepository |
| **LLMInvestigationAgent** | Generates hypotheses from evidence using LLM | `investigate(incident, evidence)` → `RootCauseAnalysis` | LLMProvider (with tool definitions) |
| **HypothesisEngine** | Generates ranked hypotheses (deterministic or LLM-backed) | `generate_hypotheses(evidence)` → list[Hypothesis] | LLMProvider (if enabled) |

### Models & Entities

**Incident** (immutable):
- `incident_id` (UUID)
- `title`, `severity` (CRITICAL, HIGH, MEDIUM, LOW)
- `status` (OPEN, INVESTIGATING, REMEDIATING, VALIDATING, RESOLVED, ESCALATED, CANCELLED)
- `affected_services` (tuple[str, ...])
- `description`, `symptoms`, `signals`
- `correlation_id` (for deduplication)
- `tenant_id`, `environment`, `tags`, `metadata`

**Evidence** (multiple per collection):
- `id`, `incident_id`
- `source_kind` (METRICS, LOGS, TRACES, DEPLOYMENT, INFRASTRUCTURE, KNOWLEDGE, OTHER)
- `source_name` (e.g., "prometheus", "elastic", "github")
- `title`, `content` (human-readable summary)
- `severity` (CRITICAL, HIGH, MEDIUM, LOW, INFO)
- `relevance_score` (0.0-1.0)
- `structured_data` (dict with provider-specific fields)
- `collected_at` (UTC datetime)

**RootCauseAnalysis**:
- `hypothesis` (str: the root cause hypothesis)
- `confidence` (0.0-1.0)
- `supporting_evidence_ids` (list[str])
- `reasoning` (str: explanation of reasoning)

**RemediationAction**:
- `action_type` (scale, rollback, deploy, restart, custom)
- `target_service` (str)
- `risk_level` (LOW, MEDIUM, HIGH, CRITICAL)
- `is_reversible` (bool)
- `status` (PENDING, EXECUTING, SUCCEEDED, FAILED)

### Database

**Primary Database:** PostgreSQL 17 (Azure Database for PostgreSQL)

**Schema:** Single table `incidents` (expanded dynamically)

**IncidentORM:**
```sql
CREATE TABLE incidents (
  incident_id        VARCHAR(255) PRIMARY KEY,
  correlation_id     VARCHAR(255) UNIQUE INDEX,
  title              TEXT NOT NULL,
  description        TEXT,
  severity           VARCHAR(50),  -- critical, high, medium, low
  status             VARCHAR(50),  -- open, investigating, remediating, validating, resolved, escalated, cancelled
  environment        VARCHAR(100),
  tenant_id          VARCHAR(255),
  
  -- JSON-serialized complex structures
  affected_services_json VARCHAR(MAX),  -- JSON list ["service1", "service2", ...]
  symptoms_json          VARCHAR(MAX),  -- JSON list ["symptom1", "symptom2", ...]
  signals_json           VARCHAR(MAX),  -- JSON serialized IncidentSignal[]
  tags_json              VARCHAR(MAX),  -- JSON list ["tag1", "tag2", ...]
  metadata_json          VARCHAR(MAX),  -- JSON object
  
  -- Timestamps (UTC, timezone-aware)
  detected_at            TIMESTAMP NOT NULL,
  created_at             TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at             TIMESTAMP NOT NULL DEFAULT NOW(),
  
  -- Indexes
  INDEX ix_incidents_status (status),
  INDEX ix_incidents_correlation_id (correlation_id),
);
```

**Repository Pattern:**
- `PostgreSQLIncidentRepository` — CRUD operations
- `BaseRepository` — Shared retry logic (exponential backoff)

**Connection Handling:**
- `DatabaseSessionManager` — Lifespan management (async context managers)
- Async-only: all operations are `async def`
- Connection pool: configurable size (default 10), max overflow (default 20)
- Retry policy: exponential backoff for transient failures (timeout, connection refused, etc.)

**Migrations:**
- Tool: Alembic
- Stored in: `backend/db/migrations/versions/`
- Initial: `0001_create_incidents.py` — Creates `incidents` table
- Driven by version control; applied on startup or manually

---

## 6. Frontend

**Status:** Not implemented  
**Location:** `frontend/` (placeholder directory only, `.gitkeep`)

No UI code exists. The platform is API-first. Incident ingestion and results retrieval are designed for programmatic access (API clients, webhooks, integrations).

---

## 7. AI / ML Components

### LLM Providers

**Selection:** `SENTINEL_LLM__PRIMARY__NAME=sarvam|mistral|openai|fake`

**Supported Providers:**

1. **Sarvam AI**
   - Endpoint: https://api.sarvam.ai/v1
   - Model: sarvam-105b (128K context, reasoning-focused)
   - Adapter: `SarvamLLMAdapter` wraps OpenAICompatibleLLMAdapter
   - Authentication: Bearer token (API key)

2. **Mistral**
   - Endpoint: https://api.mistral.ai/v1
   - Model: mistral-large-latest (or configurable)
   - Adapter: `MistralLLMAdapter`
   - Authentication: API key header

3. **OpenAI-Compatible**
   - Supports: Azure OpenAI, OpenAI, custom compatible APIs
   - Adapter: `OpenAICompatibleLLMAdapter`
   - Authentication: Bearer token

4. **Fallback Chain**
   - `FallbackLLMProvider` → Primary (Sarvam) → Fallback (Mistral) → Error
   - Enabled via `SENTINEL_LLM__ENABLE_FALLBACK=true`

5. **Fake**
   - For testing; returns pre-configured responses (no network I/O)

**Configuration:**
```python
# LLMSettings (from settings.llm)
primary: LLMProviderSettings(
    name="sarvam",
    api_key=SecretStr("..."),  # From env or KeyVault
    model="sarvam-105b",
    base_url="https://api.sarvam.ai/v1",
    timeout_seconds=60.0,
    max_retries=3,
    retry_min_wait_seconds=1.0,
    retry_max_wait_seconds=10.0,
)
fallback: LLMProviderSettings(...)
enable_fallback: bool = True
```

**LLM Request/Response:**

```python
class LLMRequest:
    messages: list[LLMMessage]  # role + content
    model: str
    temperature: float | None
    max_output_tokens: int | None
    tools: list[LLMToolDefinition]  # JSON schema
    tool_choice: "auto" | "required" | str  # Specific tool name
    structured_schema: dict[str, Any] | None  # JSON schema for structured output
    cancellation_token: asyncio.Event | None
    timeout_seconds: float | None
    correlation_id: str | None

class LLMResponse:
    content: str  # Raw text
    finish_reason: "stop" | "tool_calls" | "length" | "error"
    tool_calls: list[LLMToolCall] | None
    structured_output: dict[str, Any] | None
    usage: ProviderUsage  # input_tokens, output_tokens, total_tokens
    correlation_id: str | None
```

### Evidence Collection

**Providers:**

| Provider | Type | Implementation | Status |
|---|---|---|---|
| **PrometheusMetricsProvider** | Metrics | `backend/providers/evidence/metrics.py` | Production-ready |
| **AzureMonitorMetricsProvider** | Metrics | `backend/providers/evidence/azure_monitor.py` | Production-ready |
| **ElasticLogsProvider** | Logs | `backend/providers/evidence/logs.py` | Production-ready |
| **OTLPTracesProvider** | Traces | `backend/providers/evidence/traces.py` | Production-ready |
| **GraphRetriever** | Knowledge | `backend/retrieval/graph.py` | Optional (Neo4j) |
| **Custom** | Any | Pluggable via factory | Extensible |

**Automatic Metric Queries (when no explicit queries provided):**
- `error_rate`
- `request_rate`
- `latency_ms`
- `exception_rate`
- `dependency_failure_rate`

**Collection Flow:**
```python
collector = EvidenceCollector(
    providers=[metrics, logs, traces, knowledge],
    max_concurrency=10,
)
evidence_collection = await collector.collect(
    incident,
    source_kinds={EvidenceSourceKind.METRICS, EvidenceSourceKind.LOGS},
    max_items_per_provider=20,
)
# Evidence Collection is gracefully degraded if providers fail
```

### Investigation Agent

**LLMInvestigationAgent** (`backend/services/llm_investigation_agent.py`):

**Flow:**
1. Collect evidence from all sources
2. Normalize evidence into structured format
3. Build LLM prompt with evidence context
4. Call LLM with tool definitions:
   - `request_evidence(source_kinds)` — Agent can ask for additional evidence
5. Parse LLM response into structured RCA output (JSON schema validation)
6. Validate: all evidence IDs referenced must exist in collected evidence
7. Return `RootCauseAnalysis` with confidence score

**Configuration:**
- Tool calling enabled by default
- Max tool rounds: 3 (agent can iterate requesting evidence)
- Structured output schema enforced
- Hallucination stripping: evidence IDs not in collection removed

**Fallback:**
- If LLM fails or is misconfigured, use `DeterministicHypothesisGenerator`
- Deterministic generator uses heuristics (patterns in evidence)

---

## 8. Data Flow

### Incident Lifecycle (Main Workflow)

```
1. INGESTION
   User/Alert → POST /api/v1/incidents → IncidentIngestionService
   ├─ Validate request (Pydantic)
   ├─ Check for duplicate (correlation_id)
   ├─ Persist to PostgreSQL
   └─ Return incident_id

2. INVESTIGATION (Cycle 1)
   ClosedLoopOrchestrator.run(incident)
   ├─ InvestigationOrchestrator.investigate()
   │  ├─ EvidenceOrchestrator.collect() [parallel]
   │  │  ├─ MetricsEvidenceProvider → Evidence[]
   │  │  ├─ LogsEvidenceProvider → Evidence[]
   │  │  ├─ TracesEvidenceProvider → Evidence[]
   │  │  └─ On provider failure: graceful degradation (continue with partial)
   │  ├─ EvidenceNormalizer normalize evidence
   │  └─ LLMInvestigationAgent.investigate(incident, evidence)
   │     ├─ Build structured prompt with evidence
   │     ├─ Call LLM with tool definitions
   │     ├─ Optional: agent requests additional evidence
   │     ├─ Parse structured RCA response
   │     ├─ Validate evidence references
   │     └─ Return RootCauseAnalysis(hypothesis, confidence, supporting_evidence)
   
3. REMEDIATION (if planner wired)
   GitHubRemediationPlanner(incident, rca) → RemediationPlan
   RemediationEngine.execute_plan(plan, incident)
   └─ Policy gate: LOW/MEDIUM allowed; HIGH/CRITICAL require approval

4. DEPLOYMENT
   DeploymentPipeline.deploy(deployment_request, incident)
   └─ GitHubActionsDeploymentProvider
      ├─ POST to GitHub API: create workflow run
      ├─ Idempotency key: per-cycle deduplication
      ├─ Poll workflow status
      └─ Return DeploymentResult(succeeded, error, deployment_id)

5. VERIFICATION
   VerificationCoordinator.verify(incident, plan, deployment)
   ├─ Build VerificationPlan (e.g., check error_rate < threshold, latency_ms < 500ms)
   ├─ VerificationEngine.verify(plan)
   │  ├─ Query AzureMonitorMetricsSnapshot
   │  ├─ Collect before/after metrics
   │  ├─ Compare thresholds
   │  └─ Return VerificationOutcome(passed, metrics_before, metrics_after)
   └─ If passed → RESOLVED; if failed and cycle < 3 → REINVESTIGATE (cycle 2)

6. RESOLUTION
   Update Incident.status = RESOLVED
   Emit event: INCIDENT_RESOLVED
   Store resolution metadata
```

### Event-Driven Processing

**Event Stream (Redis Streams):**
- `RedisWorkflowEventPublisher` emits lifecycle events
- Event types: `INVESTIGATION_STARTED`, `ROOT_CAUSE_IDENTIFIED`, `REMEDIATION_ACTION_EXECUTED`, `INCIDENT_DEPLOYED`, `INCIDENT_RESOLVED`, `INCIDENT_ESCALATED`, etc.
- Consumers: optional `WorkflowEventWorker` consumes and publishes via pub/sub for real-time dashboards
- All events tagged with `incident_id`, `execution_id`, `correlation_id` for tracing

---

## 9. Authentication & Authorization

### API Authentication

**Status:** Not implemented  
**By Design:** Platform assumes upstream authentication (API Gateway, service mesh, WAF)  
**Why:** Designed for internal Kubernetes environments with network-level access control

### Infrastructure Access (Azure)

**Authentication Modes:**

1. **Default (Managed Identity)** — Primary for production
   ```python
   DefaultAzureCredential(
       managed_identity_client_id="...",  # Optional; searched if not specified
       exclude_environment_credential=False,  # Try env vars first
       exclude_managed_identity_credential=False,  # Try Managed Identity
   )
   ```
   Flow: Environment variables → Managed Identity → Shared token cache → Interactive login

2. **Client Secret (Service Principal)** — For CI/CD
   ```python
   ClientSecretCredential(
       tenant_id="...",
       client_id="...",
       client_secret=SecretStr("..."),
   )
   ```

**Configured via:**
```python
# settings.azure
AzureCredentialSettings(
    authentication_mode="default" | "client_secret",
    tenant_id: str | None,
    client_id: str | None,
    client_secret: SecretStr | None,
    managed_identity_client_id: str | None,
)
```

### Secrets Management

**Azure KeyVault:**
- Fetches bootstrap secrets on startup
- PostgreSQL password: key `settings.key_vault.postgres_password_secret`
- LLM API key: key `settings.key_vault.llm_primary_api_key_secret`

**Pydantic SecretStr:**
- Never logged or exposed in errors
- Redacted in repr() and str()

**GitHub Deployment Auth:**
- GitHub token (PAT or GitHub App)
- Stored in environment or KeyVault
- Used for workflow triggering and PR creation

---

## 10. Configuration & Environment Variables

**Configuration Loading (Priority Order):**
1. Explicit kwargs (init_settings)
2. Environment variables (`SENTINEL_*`)
3. `.env` file (development)
4. Nested secret files from `/run/secrets/` (Docker Secrets, Kubernetes)

**Environment Variable Naming:**
- Prefix: `SENTINEL_`
- Nested delimiter: `__` (e.g., `SENTINEL_POSTGRES__POOL_SIZE`)
- Case-insensitive

| Variable | Purpose | Required | Used By | Default |
|---|---|---|---|---|
| `SENTINEL_ENVIRONMENT` | Deployment context | Yes | AppSettings.environment | development |
| `SENTINEL_DEBUG` | Debug mode | No | AppSettings.debug | false (production enforces false) |
| `SENTINEL_APP_VERSION` | App version string | No | AppSettings.app_version | 0.1.0 |
| `SENTINEL_SERVER__HOST` | HTTP bind address | No | ServerSettings.host | 0.0.0.0 |
| `SENTINEL_SERVER__PORT` | HTTP port | No | ServerSettings.port | 8000 |
| `SENTINEL_SERVER__WORKERS` | Uvicorn workers | No | ServerSettings.workers | 1 |
| `SENTINEL_LOGGING__LEVEL` | Log level | No | LoggingSettings.level | INFO |
| `SENTINEL_LOGGING__JSON_OUTPUT` | JSON logging | No | LoggingSettings.json_output | true (production enforces true) |
| `SENTINEL_POSTGRES__HOST` | PostgreSQL hostname | No | PostgresSettings.host | localhost |
| `SENTINEL_POSTGRES__PORT` | PostgreSQL port | No | PostgresSettings.port | 5432 |
| `SENTINEL_POSTGRES__DATABASE` | Database name | No | PostgresSettings.database | sentinel |
| `SENTINEL_POSTGRES__USERNAME` | DB username | No | PostgresSettings.username | sentinel |
| `SENTINEL_POSTGRES__PASSWORD` | DB password | No | PostgresSettings.password | sentinel (INSECURE!) |
| `SENTINEL_POSTGRES__POOL_SIZE` | Connection pool size | No | PostgresSettings.pool_size | 10 |
| `SENTINEL_POSTGRES__MAX_OVERFLOW` | Pool overflow | No | PostgresSettings.max_overflow | 20 |
| `SENTINEL_REDIS__HOST` | Redis hostname | No | RedisSettings.host | localhost |
| `SENTINEL_REDIS__PORT` | Redis port | No | RedisSettings.port | 6379 |
| `SENTINEL_REDIS__DATABASE` | Redis DB number | No | RedisSettings.database | 0 |
| `SENTINEL_REDIS__PASSWORD` | Redis password | No | RedisSettings.password | None |
| `SENTINEL_REDIS__SSL` | Redis SSL | No | RedisSettings.ssl | false |
| `SENTINEL_NEO4J__ENABLED` | Enable Neo4j | No | Neo4jSettings.enabled | false |
| `SENTINEL_NEO4J__URI` | Neo4j connection URI | No | Neo4jSettings.uri | neo4j://localhost:7687 |
| `SENTINEL_NEO4J__DATABASE` | Neo4j database | No | Neo4jSettings.database | neo4j |
| `SENTINEL_NEO4J__USERNAME` | Neo4j username | No | Neo4jSettings.username | neo4j |
| `SENTINEL_NEO4J__PASSWORD` | Neo4j password | No | Neo4jSettings.password | neo4j |
| `SENTINEL_COSMOS__ENABLED` | Enable Cosmos | No | CosmosSettings.enabled | false |
| `SENTINEL_COSMOS__ENDPOINT` | Cosmos endpoint | No | CosmosSettings.endpoint | None |
| `SENTINEL_COSMOS__DATABASE_NAME` | Cosmos database | No | CosmosSettings.database_name | sentinel |
| `SENTINEL_COSMOS__CONTAINER_NAME` | Cosmos container | No | CosmosSettings.container_name | sentinel |
| `SENTINEL_BLOB__ENABLED` | Enable Blob Storage | No | BlobStorageSettings.enabled | false |
| `SENTINEL_BLOB__ACCOUNT_URL` | Blob account URL | No | BlobStorageSettings.account_url | None |
| `SENTINEL_BLOB__CONTAINER_NAME` | Blob container | No | BlobStorageSettings.container_name | sentinel |
| `SENTINEL_KEY_VAULT__ENABLED` | Enable KeyVault | No | KeyVaultSettings.enabled | false |
| `SENTINEL_KEY_VAULT__URL` | KeyVault URL | No | KeyVaultSettings.url | None |
| `SENTINEL_KEY_VAULT__POSTGRES_PASSWORD_SECRET` | KV secret name for PG pwd | No | KeyVaultSettings.postgres_password_secret | postgres-admin-password |
| `SENTINEL_KEY_VAULT__LLM_PRIMARY_API_KEY_SECRET` | KV secret name for LLM key | No | KeyVaultSettings.llm_primary_api_key_secret | llm-primary-api-key |
| `SENTINEL_LLM__PRIMARY__NAME` | Primary LLM provider | No | LLMSettings.primary.name | fake |
| `SENTINEL_LLM__PRIMARY__API_KEY` | Primary LLM API key | No | LLMSettings.primary.api_key | None (required for production) |
| `SENTINEL_LLM__PRIMARY__MODEL` | Primary LLM model | No | LLMSettings.primary.model | sarvam-105b |
| `SENTINEL_LLM__PRIMARY__BASE_URL` | Primary LLM endpoint | No | LLMSettings.primary.base_url | https://api.sarvam.ai/v1 |
| `SENTINEL_LLM__PRIMARY__TIMEOUT_SECONDS` | LLM timeout | No | LLMSettings.primary.timeout_seconds | 60.0 |
| `SENTINEL_LLM__FALLBACK__NAME` | Fallback LLM provider | No | LLMSettings.fallback.name | mistral |
| `SENTINEL_LLM__FALLBACK__API_KEY` | Fallback LLM API key | No | LLMSettings.fallback.api_key | None |
| `SENTINEL_LLM__ENABLE_FALLBACK` | Enable fallback chain | No | LLMSettings.enable_fallback | true |
| `SENTINEL_EVIDENCE__METRICS__NAME` | Metrics provider | No | EvidenceSettings.metrics.name | prometheus |
| `SENTINEL_EVIDENCE__METRICS__BASE_URL` | Metrics provider URL | No | EvidenceSettings.metrics.base_url | http://prometheus:9090 |
| `SENTINEL_EVIDENCE__METRICS__API_KEY` | Metrics provider API key | No | EvidenceSettings.metrics.api_key | None |
| `SENTINEL_EVIDENCE__LOGS__NAME` | Logs provider | No | EvidenceSettings.logs.name | elastic |
| `SENTINEL_EVIDENCE__LOGS__BASE_URL` | Logs provider URL | No | EvidenceSettings.logs.base_url | http://elasticsearch:9200 |
| `SENTINEL_EVIDENCE__TRACES__NAME` | Traces provider | No | EvidenceSettings.traces.name | otlp |
| `SENTINEL_EVIDENCE__TRACES__BASE_URL` | Traces provider URL | No | EvidenceSettings.traces.base_url | http://jaeger:16686 |
| `SENTINEL_GITHUB__ENABLED` | Enable GitHub integration | No | GitHubSettings.enabled | false |
| `SENTINEL_GITHUB__TOKEN` | GitHub token (PAT/App) | No | GitHubSettings.token | None |
| `SENTINEL_GITHUB__DEFAULT_OWNER` | GitHub org/owner | No | GitHubSettings.default_owner | — |
| `SENTINEL_GITHUB__DEFAULT_REPOSITORY` | GitHub repository | No | GitHubSettings.default_repository | — |
| `SENTINEL_DEPLOYMENT__ENABLED` | Enable deployment pipeline | No | DeploymentSettings.enabled | false |
| `SENTINEL_DEPLOYMENT__PROVIDER` | Deployment provider | No | DeploymentSettings.provider | fake |
| `SENTINEL_DEPLOYMENT__WORKFLOW` | GitHub workflow file | No | DeploymentSettings.workflow | deploy.yml |
| `SENTINEL_DEPLOYMENT__ENVIRONMENT` | Deployment target env | No | DeploymentSettings.environment | staging |
| `SENTINEL_FEATURE_FLAGS__GRAPH_RAG` | Enable GraphRAG | No | FeatureFlagSettings.graph_rag | false |
| `SENTINEL_FEATURE_FLAGS__EVALUATION_PIPELINE` | Enable evaluation | No | FeatureFlagSettings.evaluation_pipeline | false |
| `SENTINEL_FEATURE_FLAGS__RECOVERY_ENGINE` | Enable recovery engine | No | FeatureFlagSettings.recovery_engine | false |
| `SENTINEL_FEATURE_FLAGS__NOTIFICATION_DELIVERY` | Enable notifications | No | FeatureFlagSettings.notification_delivery | false |
| `SENTINEL_FEATURE_FLAGS__AUDIT_TRAIL` | Enable audit trail | No | FeatureFlagSettings.audit_trail | true |
| `SENTINEL_OPENTELEMETRY__ENABLED` | Enable OpenTelemetry | No | OpenTelemetrySettings.enabled | true |
| `SENTINEL_OPENTELEMETRY__OTLP_HTTP_ENDPOINT` | OTLP HTTP endpoint | No | OpenTelemetrySettings.otlp_http_endpoint | None |
| `SENTINEL_OPENTELEMETRY__AZURE_MONITOR_CONNECTION_STRING` | App Insights conn string | No | OpenTelemetrySettings.azure_monitor_connection_string | None |
| `SENTINEL_AZURE__AUTHENTICATION_MODE` | Azure auth method | No | AzureCredentialSettings.authentication_mode | default |
| `SENTINEL_AZURE__TENANT_ID` | Azure tenant ID | No | AzureCredentialSettings.tenant_id | None |
| `SENTINEL_AZURE__CLIENT_ID` | Azure client ID | No | AzureCredentialSettings.client_id | None |
| `SENTINEL_AZURE__CLIENT_SECRET` | Azure client secret | No | AzureCredentialSettings.client_secret | None |
| `SENTINEL_AZURE__MANAGED_IDENTITY_CLIENT_ID` | Azure MI client ID | No | AzureCredentialSettings.managed_identity_client_id | None |
| `SENTINEL_SECRETS_DIR` | Secrets directory (K8s) | No | Pydantic settings source | /run/secrets |

**Production Validation:**
- `debug=true` in PRODUCTION → Error
- `logging.json_output=false` in PRODUCTION → Error

---

## 11. External Integrations

### LLM Providers

| Provider | API | Purpose | Required Env Vars | Failure Mode |
|---|---|---|---|---|
| **Sarvam AI** | https://api.sarvam.ai/v1 | Primary LLM (reasoning) | `SENTINEL_LLM__PRIMARY__API_KEY` | Fall back to Mistral or Fake |
| **Mistral** | https://api.mistral.ai/v1 | Fallback LLM | `SENTINEL_LLM__FALLBACK__API_KEY` | Error; no further fallback |
| **Azure OpenAI** | Azure-specific | Alternative (custom base_url) | `SENTINEL_LLM__PRIMARY__API_KEY` | Same as OpenAI-compatible |
| **OpenAI** | https://api.openai.com/v1 | Alternative (custom base_url) | `SENTINEL_LLM__PRIMARY__API_KEY` | Fallback chain |

### Evidence Sources

| Source | API/Protocol | Purpose | Required Env Vars | Fallback |
|---|---|---|---|---|
| **Prometheus** | HTTP /api/v1/query_range | Metrics collection | `SENTINEL_EVIDENCE__METRICS__BASE_URL` | Graceful degradation (no metrics) |
| **Azure Monitor** | REST API | Metrics collection | `SENTINEL_EVIDENCE__METRICS__BASE_URL`, `API_KEY` | Graceful degradation |
| **Elastic** | HTTP /_search | Logs collection | `SENTINEL_EVIDENCE__LOGS__BASE_URL` | Graceful degradation |
| **Jaeger/OTLP** | gRPC/HTTP | Traces collection | `SENTINEL_EVIDENCE__TRACES__BASE_URL` | Graceful degradation |
| **GitHub** | REST API | Deployments, PRs | `SENTINEL_GITHUB__TOKEN` | Error (HIGH risk) |

### Cloud Services

| Service | Purpose | Required Env Vars | Auth |
|---|---|---|---|
| **Azure KeyVault** | Bootstrap secrets | `SENTINEL_KEY_VAULT__URL` | Managed Identity / Service Principal |
| **Azure PostgreSQL** | Primary database | `SENTINEL_POSTGRES__URL` | Connection string or Managed Identity |
| **Azure Cache for Redis** | Cache/streams/locks | `SENTINEL_REDIS__URL` | Connection string or redis-entraid (Managed Identity) |
| **Azure Cosmos DB** | Document store (optional) | `SENTINEL_COSMOS__ENDPOINT` | Managed Identity |
| **Azure Blob Storage** | Object storage (optional) | `SENTINEL_BLOB__ACCOUNT_URL` | Managed Identity |
| **Azure Application Insights** | Observability (optional) | `SENTINEL_OPENTELEMETRY__AZURE_MONITOR_CONNECTION_STRING` | Connection string |

### Deployment & CI/CD

| Service | Purpose | Required Env Vars | Auth |
|---|---|---|---|
| **GitHub Actions** | Workflow triggering | `SENTINEL_GITHUB__TOKEN`, `SENTINEL_DEPLOYMENT__WORKFLOW` | GitHub token (PAT / GitHub App) |
| **GitHub API** | Deployment history, PR creation | `SENTINEL_GITHUB__TOKEN` | GitHub token |
| **Azure Container Apps** | Deployment target (production) | Azure subscription + resource group | Workload identity federation via OIDC |
| **Azure Container Registry** | Image storage | Azure subscription | Managed Identity |

---

## 12. Infrastructure & Deployment

### Local Development

**docker-compose.yml:**
```yaml
services:
  api:      # FastAPI app (port 8000)
  postgres: # PostgreSQL 17-alpine (port 5432)
  redis:    # Redis 7.4-alpine (port 6379)
  neo4j:    # Neo4j 5.26-community (port 7687) — optional
```

**Startup:**
```bash
# Install dependencies
uv sync --frozen

# Start local stack
docker-compose up -d

# Run migrations
alembic upgrade head

# Start API
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Run tests
pytest -q

# Lint & type check
ruff check .
mypy .
```

### Production Deployment

**Infrastructure:**
- **Container Image:** Built from `Dockerfile` (multi-stage, uv-based)
- **Container Registry:** Azure Container Registry (`sentinelacr20261003`)
- **Orchestration:** Azure Container Apps (managed containers)
- **Database:** Azure Database for PostgreSQL (managed)
- **Cache:** Azure Cache for Redis (managed)
- **Secrets:** Azure KeyVault (managed)
- **Observability:** Azure Application Insights (optional)

**Deployment Process (GitHub Actions):**

Workflow file: `.github/workflows/deploy-azure.yml`

Steps:
1. **Setup:** Python 3.12 + uv
2. **Install:** `uv sync --frozen`
3. **Test:** `pytest`
4. **Lint:** `ruff check .`
5. **Type:** `mypy .`
6. **Azure Login:** Workload identity federation (OIDC)
7. **Build Docker image:** Push to Azure Container Registry
8. **Deploy:** Update Azure Container App with new image (rolling update)

**Configuration:**
- Environment variables: passed to Container App via Configuration → Environment variables
- Secrets: stored in KeyVault; referenced as `@Microsoft.KeyVault(SecretUri=...)`
- Network: internal (not exposed directly; typically behind API Gateway)

**Scaling:**
- Container Apps scales on CPU/memory thresholds
- Database: Azure PostgreSQL auto-scaling tier
- Redis: Azure Cache for Redis Standard/Premium tier

### Container Architecture

**Dockerfile:**
```dockerfile
# Stage 1: uv installation
FROM python:3.12 AS base
RUN pip install uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Stage 2: Runtime
FROM python:3.12-slim
RUN groupadd -g 10001 sentinel && useradd -u 10001 -g sentinel sentinel

WORKDIR /app
COPY --from=base /app/.venv /app/.venv
COPY . .

USER sentinel
EXPOSE 8000

HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## 13. Testing

**Framework:** pytest 8.3+ with pytest-asyncio  
**Type Checking:** mypy 1.14+ (strict mode)  
**Linting:** Ruff 0.9+  
**Coverage:** pytest-cov (800+ tests currently passing)

**Test Structure:**
```
backend/tests/
├── test_configuration.py                   # Settings loading & environment
├── test_llm_investigation_agent.py          # LLM agent behavior, tool use
├── test_key_vault_llm_integration.py        # KeyVault secret resolution
├── test_rag_sre_knowledge.py                # RAG knowledge retrieval
├── test_redis.py                           # Redis integration (5 tests)
├── test_postgres_incident_repository.py    # Database persistence
├── test_incident_api.py                    # HTTP API routes
├── test_incident_lifecycle.py              # Closed-loop orchestration
├── test_closed_loop.py                     # Full incident resolution flow
├── test_deployment.py                      # Deployment pipeline
├── test_azure_monitor_snapshot.py          # Metrics snapshot (verification)
├── test_llm_providers.py                   # LLM adapters (Sarvam, Mistral, OpenAI)
├── test_evidence_providers.py              # Evidence collection (Prometheus, Elastic, OTLP)
├── test_tools.py                           # Tool/action execution
├── test_workflows.py                       # Workflow execution & SRE rules
├── test_sre_workflow.py                    # SRE-specific workflows
├── test_database.py                        # Database operations
├── test_health.py                          # Health checks
├── test_telemetry.py                       # OpenTelemetry instrumentation
├── conftest_db.py                          # Fixtures (async sessions, factories)
├── test_agents.py                          # Agent framework
├── test_runtime_core.py                    # Runtime execution
└── ... (40+ test files)
```

**Unit Tests:** Isolated component testing with mocks  
**Integration Tests:** Multi-component testing with test doubles (FakeLLMProvider, FakeDeploymentProvider)  
**API Tests:** HTTP endpoint validation

**Running Tests:**
```bash
# All tests
pytest -q

# Specific test file
pytest backend/tests/test_llm_investigation_agent.py -v

# With coverage
pytest --cov=backend --cov-report=html

# Watch mode (not standard with pytest; use pytest-watch)
ptw
```

**Test Doubles:**
- `FakeLLMProvider` — Pre-configured responses (no network I/O)
- `FakeDeploymentProvider` — Mock GitHub Actions (no actual deployments)
- `FakeMetricsProvider`, `FakeLogsProvider`, `FakeTracesProvider` — Mock evidence sources
- In-memory test database (PostgreSQL test container or SQLite for unit tests)

---

## 14. Observability

### Tracing

**Technology:** OpenTelemetry 1.29+

**Exporters:**
1. **OTLP HTTP** — Generic observability backends (Jaeger, Tempo, etc.)
2. **Azure Monitor** — Application Insights integration

**Instrumentation (Auto):**
- FastAPI endpoints
- SQLAlchemy database queries
- httpx HTTP calls (LLM, evidence providers)
- Redis operations

**Trace Context Propagation:**
- Correlation ID: `incident_id`, `execution_id`, `correlation_id`
- All spans tagged with trace context

### Metrics

**Technology:** Prometheus Client 0.21+

**Endpoint:** `GET /metrics` (Prometheus-compatible format)

**Metrics Exported:**
- HTTP request latency (by endpoint, method, status)
- Database query latency (by operation)
- Redis operation latency (by command)
- LLM provider latency (by provider, model)
- Incident processing duration (by phase)
- Evidence collection latency (by provider)

### Logging

**Technology:** structlog 24.4+

**Output Modes:**
- **Development:** Human-readable text (one per line)
- **Production:** JSON (configured via `SENTINEL_LOGGING__JSON_OUTPUT=true`)

**Log Levels:** DEBUG, INFO, WARNING, ERROR, CRITICAL

**Bound Context:** All logs include `incident_id`, `execution_id`, `correlation_id` when available

**Sample Log:**
```json
{
  "timestamp": "2026-10-08T15:30:45.123Z",
  "level": "info",
  "event": "closed_loop_resolved",
  "incident_id": "incident-abc123",
  "execution_id": "exec-xyz789",
  "cycle": 1,
  "duration_ms": 2345.67,
  "service": "sentinel"
}
```

### Health Checks

**Endpoints:**
- `GET /health` — Service identity (no I/O)
- `GET /health/live` — Liveness (always 200)
- `GET /health/ready` — Readiness (queries all backends)

**Backends Probed (if enabled):**
- PostgreSQL (SELECT 1)
- Redis (PING)
- Neo4j (cypher query)
- Cosmos (metadata query)

**Timeout:** configurable per backend (default 3 seconds)

---

## 15. Security

### Implemented Protections

✅ **Secret handling:**
- Pydantic `SecretStr` for all credentials (never logged)
- Azure KeyVault for bootstrap secrets
- Environment variables for configuration

✅ **Authentication (Infrastructure):**
- Azure Managed Identity for all Azure services
- Service Principal support for CI/CD

✅ **Authorization (Policy):**
- Risk-level gates on remediation actions (HIGH/CRITICAL require approval)
- Policy engine evaluates action approvals

✅ **Database:**
- SQLAlchemy parameterized queries (SQL injection safe)
- Async-only operations (no blocking calls that could accumulate connections)

✅ **Input Validation:**
- Pydantic models validate all API requests
- Type checking (mypy) for static analysis

✅ **HTTPS/TLS:**
- Redis: optional SSL (`SENTINEL_REDIS__SSL=true`)
- Database: connection string supports SSL
- All external API calls use HTTPS

✅ **Secrets in Configuration:**
- No secrets hardcoded in source
- Environment variables use SecretStr wrappers
- KeyVault integration for production

### Not Implemented

❌ **API authentication:** Upstream responsibility (API Gateway, service mesh)  
❌ **Rate limiting:** Not present; assumes upstream protection  
❌ **CORS:** Not configured; assumes frontend on same origin or handled upstream  
❌ **CSRF protection:** Not applicable (API-only, stateless)  
❌ **Approval workflow UI:** Gate logic exists, but no system for approving HIGH/CRITICAL actions  

### Potential Vulnerabilities

⚠️ **LLM prompt injection:** Evidence strings are incorporated into LLM prompts without sanitization; malicious evidence could potentially manipulate LLM reasoning  
⚠️ **Deployment webhook forgery:** GitHub webhook signature verification (if implemented) not explicitly documented  
⚠️ **Idempotency key collision:** Using `correlation_id + cycle` for idempotency; low collision risk but not cryptographically strong  

---

## 16. Current State

### Implemented ✅

- Core closed-loop orchestration (investigation → remediation → deployment → verification)
- PostgreSQL incident store with full CRUD
- Redis streams for event publishing
- LLM investigation agents with multiple providers (Sarvam, Mistral, OpenAI-compatible)
- Evidence collection from Prometheus, Elastic, OTLP, GitHub, knowledge base
- Deterministic and LLM-backed RCA generation
- GitHub Actions deployment integration
- Azure Monitor metrics verification
- Azure KeyVault secret management
- OpenTelemetry tracing and metrics
- Structured logging with JSON output
- Comprehensive test suite (800 tests passing)
- Production-ready Docker image and ACA deployment

### Partially Implemented ⚠️

- GraphRAG (implemented but feature-flagged off; Neo4j optional)
- Evaluation pipeline (framework exists, evaluation strategies incomplete)
- Approval workflows for HIGH/CRITICAL actions (gate logic present, tracking/UI not implemented)
- Notification delivery (feature flag exists, implementation absent)

### Not Implemented ❌

- Frontend UI (placeholder directory only)
- API-level authentication (assumed upstream)
- Scheduled tasks (scheduler module empty)
- Approval submission/tracking system
- Notification integrations (Slack, email, PagerDuty, etc.)

### TODOs / FIXMEs

Extract from codebase (not exhaustive):
- `backend/services/closed_loop_orchestrator.py`: "Implement verification planner factory"
- `backend/core/hypothesis_engine.py`: "Add confidence boosting heuristics"
- `backend/providers/github/client.py`: "Add webhook signature verification"
- Various: "Add metrics for X phase" (observability gaps)
- `backend/evaluation/`: "Complete strategy registry"

### Known Issues

1. **GraphRAG disabled:** Neo4j integration is complete but feature-flagged off (`FEATURE_FLAGS__GRAPH_RAG=false`)
2. **Approval tracking absent:** Policy gates exist (block/allow), but no mechanism to track who approved/rejected HIGH/CRITICAL actions
3. **Scheduler dormant:** `backend/scheduler/` module exists but is unused; all orchestration is event-driven
4. **Notification system not wired:** Feature flag and settings exist but no implementation
5. **Limited evidence providers:** Only Prometheus/Azure Monitor (metrics), Elastic (logs), OTLP (traces); no cloud-native log services (CloudWatch, Datadog, Splunk) out of the box
6. **Deterministic hypothesis fallback:** If LLM fails, falls back to heuristic generator; no sophisticated fallback reasoning

---

## 17. Important Files

| File | Importance | Purpose |
|---|---|---|
| `main.py` | **Critical** | ASGI entry point |
| `backend/application/factory.py` | **Critical** | FastAPI app factory and initialization |
| `backend/application/providers.py` | **Critical** | Dishka DI container setup |
| `backend/application/container.py` | **Critical** | Resource lifecycle (db, redis, azure creds) |
| `backend/services/closed_loop_orchestrator.py` | **Critical** | Incident lifecycle orchestration engine |
| `backend/configuration/settings.py` | **Critical** | All configuration and environment variables |
| `backend/models/incident.py` | **Critical** | Incident domain model |
| `backend/db/models/incident.py` | **Critical** | Incident ORM (PostgreSQL) |
| `backend/api/routers/incidents.py` | **Critical** | Incident API endpoints |
| `backend/services/investigation_orchestrator.py` | **High** | Evidence collection → RCA generation |
| `backend/services/llm_investigation_agent.py` | **High** | LLM-backed investigation |
| `backend/services/verification_coordinator.py` | **High** | Post-deployment health checks |
| `backend/providers/evidence/azure_monitor.py` | **High** | Azure Monitor metrics provider |
| `backend/providers/llm/factory.py` | **High** | LLM provider selection and wiring |
| `backend/interfaces/llm.py` | **High** | LLM provider contract (protocol) |
| `backend/interfaces/evidence.py` | **High** | Evidence provider contract |
| `backend/infrastructure/redis.py` | **High** | Redis client creation and connection |
| `backend/db/session.py` | **High** | Database session management |
| `backend/db/engine.py` | **High** | SQLAlchemy engine creation |
| `backend/queues/redis.py` | **High** | Redis streams, pub/sub, locks |
| `backend/events/workflow_streams.py` | **High** | Event publishing |
| `backend/providers/github/client.py` | **High** | GitHub API integration |
| `backend/providers/github/deployment.py` | **High** | GitHub Actions deployment provider |
| `backend/providers/metrics/azure_monitor_snapshot.py` | **High** | Application Insights metrics |
| `pyproject.toml` | **High** | Project dependencies and metadata |
| `docker-compose.yml` | **Medium** | Local development stack |
| `Dockerfile` | **Medium** | Production image definition |
| `.github/workflows/deploy-azure.yml` | **Medium** | CI/CD pipeline |
| `backend/tests/test_llm_investigation_agent.py` | **Medium** | LLM agent tests (12 tests) |
| `backend/tests/test_rag_sre_knowledge.py` | **Medium** | RAG/knowledge tests (9 tests) |

---

## 18. Key Classes, Functions & Modules

| Symbol | File | Purpose | Used By |
|---|---|---|---|
| `ClosedLoopOrchestrator` | `backend/services/closed_loop_orchestrator.py` | Main incident lifecycle driver | HTTP handlers, workers |
| `InvestigationOrchestrator` | `backend/services/investigation_orchestrator.py` | Evidence collection → RCA | ClosedLoopOrchestrator |
| `EvidenceOrchestrator` | `backend/services/evidence_orchestrator.py` | Parallel evidence collection | InvestigationOrchestrator |
| `RemediationEngine` | `backend/services/remediation_engine.py` | Policy-gated action execution | ClosedLoopOrchestrator |
| `DeploymentPipeline` | `backend/services/deployment_pipeline.py` | GitHub Actions trigger | ClosedLoopOrchestrator |
| `VerificationCoordinator` | `backend/services/verification_coordinator.py` | Post-deployment health checks | ClosedLoopOrchestrator |
| `LLMInvestigationAgent` | `backend/services/llm_investigation_agent.py` | LLM-backed root cause generation | InvestigationOrchestrator |
| `HypothesisEngine` | `backend/core/hypothesis_engine.py` | Hypothesis generation | InvestigationOrchestrator, LLMInvestigationAgent |
| `AzureMonitorMetricsProvider` | `backend/providers/evidence/azure_monitor.py` | Azure Monitor metrics collection | EvidenceOrchestrator |
| `PrometheusMetricsProvider` | `backend/providers/evidence/metrics.py` | Prometheus metrics collection | EvidenceOrchestrator |
| `OpenAICompatibleLLMAdapter` | `backend/providers/llm/openai_compat.py` | Generic OpenAI-compatible LLM | LLMInvestigationAgent, fallback chain |
| `FallbackLLMProvider` | `backend/providers/llm/fallback.py` | Primary → Fallback LLM chain | DI container |
| `GitHubActionsDeploymentProvider` | `backend/providers/github/deployment.py` | GitHub Actions workflow trigger | DeploymentPipeline |
| `AzureMonitorMetricsSnapshot` | `backend/providers/metrics/azure_monitor_snapshot.py` | Application Insights metrics query | VerificationEngine |
| `PostgreSQLIncidentRepository` | `backend/db/repositories/incident.py` | Incident CRUD operations | Services, HTTP handlers |
| `DatabaseSessionManager` | `backend/db/session.py` | Async session lifecycle | DI container |
| `RedisConnection` | `backend/infrastructure/redis.py` | Redis client wrapper | All services needing caching/locks |
| `RedisWorkflowEventPublisher` | `backend/events/workflow_streams.py` | Event publishing to Redis stream | Services |
| `ApplicationContainer` | `backend/application/container.py` | Resource lifecycle holder | Startup, shutdown |
| `ApplicationProvider` | `backend/application/providers.py` | Dishka DI definitions | Dishka container |
| `build_api_router()` | `backend/api/router.py` | Compose all API routes | FastAPI app |
| `build_llm_provider()` | `backend/providers/llm/factory.py` | Select and wire LLM | DI container |
| `build_metrics_provider()` | `backend/providers/evidence/factory.py` | Select evidence provider | DI container |
| `create_application()` | `backend/application/factory.py` | Full app initialization | main.py |
| `load_settings()` | `backend/configuration/settings.py` | Load config from environment | Application startup |
| `Incident` | `backend/models/incident.py` | Incident domain model | Services, API |
| `Evidence` | `backend/models/evidence.py` | Evidence item | Collection, investigation |
| `RootCauseAnalysis` | `backend/models/hypothesis.py` | RCA result | Investigation, deployment decision |
| `IncidentORM` | `backend/db/models/incident.py` | Incident database model | Repository layer |

---

## 19. API / Interface Contracts

### REST API

**Request/Response Pattern:**
- All endpoints return JSON
- Errors: `{"detail": "error message"}` with appropriate HTTP status

**Incident Ingestion:**
```python
# POST /api/v1/incidents
Request: CreateIncidentRequest
Response: CreateIncidentResponse (201) | ErrorResponse (400, 409)

# GET /api/v1/incidents/{incident_id}
Request: —
Response: Incident (200) | ErrorResponse (404)

# GET /api/v1/incidents?status=...&severity=...&limit=...&offset=...
Request: Query parameters
Response: ListIncidentsResponse (200)
```

### Internal Service Contracts

**ClosedLoopOrchestrator.run():**
```python
async def run(
    incident: Incident,
    *,
    execution_id: str | None = None,
    cancellation_check: Callable[[], bool] | None = None,
    context: dict[str, Any] | None = None,
) -> ClosedLoopResult:
    """
    Returns:
      ClosedLoopResult(
        status="resolved" | "escalated" | "failed" | "cancelled",
        cycles=int,
        last_outcome: VerificationOutcome | None,
        deployment_result: DeploymentResult | None,
        resolution: IncidentResolution | None,
        duration_ms: float,
        failure_reason: str | None,
      )
    Raises: None (all errors captured in result.status)
    """
```

**LLMProvider.generate():**
```python
class LLMProvider(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResponse:
        """
        Raises: LLMError, LLMUnavailableError, LLMTimeoutError, LLMCancelledError
        """

    async def generate_structured(
        self,
        request: LLMRequest,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Structured output; raises same errors plus LLMInvalidRequestError"""
```

**EvidenceProvider.collect():**
```python
class MetricsEvidenceProvider(Protocol):
    async def collect(
        self,
        incident: Incident,
        *,
        max_items: int = 20,
        context: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        """
        Raises: EvidenceProviderError, EvidenceTimeoutError (gracefully caught)
        Returns: list[Evidence] (may be empty if provider fails)
        """
```

---

## 20. Development Commands

```bash
# Setup
uv sync --frozen                          # Install dependencies

# Development
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
docker-compose up -d                       # Start local stack (postgres, redis, neo4j)
alembic upgrade head                       # Run migrations

# Testing
pytest -q                                  # All tests (800+)
pytest -k test_llm_investigation_agent     # Specific test file
pytest --cov=backend --cov-report=html     # Coverage report

# Code Quality
ruff check .                               # Linting
mypy .                                    # Type checking
ruff format .                              # Auto-format

# Deployment (local Docker)
docker build -t sentinel-ai:latest .
docker run -p 8000:8000 sentinel-ai:latest

# Deployment (Azure Container Apps) — handled by GitHub Actions
# CI/CD pipeline: .github/workflows/deploy-azure.yml
```

---

## 21. Architectural Decisions

| Decision | Evidence | Reason | Implications |
|---|---|---|---|
| **Incident-type agnostic** | No failure-mode branching in orchestration code | Maximizes reusability across diverse failure classes | Relies on generic evidence + AI reasoning; may miss specialized domain knowledge |
| **Bounded reinvestigation loops** | `max_reinvestigation_cycles=3` hardcoded | Prevent remediation storms and cost explosion | If 3 cycles insufficient, escalation required (no human interaction) |
| **Event-driven architecture** | Redis Streams, pub/sub, no polling | High throughput, decoupled components, audit trail | Requires careful ordering guarantees (not strictly guaranteed by Redis) |
| **Pydantic for validation** | All API/config use Pydantic models | Type safety, automatic serialization, field validation | Slight runtime overhead for every request |
| **Async-only backend** | No synchronous I/O anywhere | Scalability, resource efficiency | Steeper learning curve for contributors; incompatible with blocking libraries |
| **Deterministic + LLM investigation** | Both DeterministicHypothesisGenerator and LLMInvestigationAgent | Fallback if LLM unavailable; deterministic for testing | Adds complexity; LLM hypothesis may contradict heuristic |
| **Policy-gated remediation** | RemediationPolicyEngine evaluates risk_level | Safety: prevent autonomous dangerous actions | Requires approval mechanism (not fully implemented) |
| **Graceful degradation on provider failure** | EvidenceOrchestrator continues if one provider fails | Robustness | May investigate with incomplete evidence; false RCAs possible |
| **PostgreSQL + Redis split** | PostgreSQL for transactional incident store, Redis for ephemeral events | Clear separation of concerns | Slightly higher operational complexity |
| **Feature flags over feature branches** | `FEATURE_FLAGS__GRAPH_RAG`, etc. | Safe rollout, A/B testing | Dead code accumulation if not cleaned up |

---

## 22. Technical Debt & Improvement Opportunities

### Critical

1. **Approval workflow not implemented**
   - Problem: HIGH/CRITICAL actions have policy gates but no tracking system
   - Evidence: `RemediationPolicyEngine` blocks execution, but no way to submit/track approvals
   - Recommendation: Build approval queue + webhook integration for human review

2. **LLM prompt injection risk**
   - Problem: Evidence strings (user-controlled signals) directly incorporated into LLM prompts
   - Evidence: `EvidenceNormalizer` builds prompt from raw evidence without escaping
   - Recommendation: Sanitize/escape evidence content before LLM ingestion

3. **Idempotency key collision**
   - Problem: Using `correlation_id + cycle` for idempotency; not cryptographically strong
   - Evidence: Similar format to many other keys; low collision risk but not guaranteed
   - Recommendation: Use UUID-based idempotency key, keyed by `(correlation_id, cycle)`

### High

4. **GraphRAG disabled but partially implemented**
   - Problem: Neo4j integration exists but is feature-flagged off; dead code accumulation
   - Evidence: `backend/retrieval/graph.py`, `Neo4jCypherExecutor`, but `FEATURE_FLAGS__GRAPH_RAG=false`
   - Recommendation: Either complete GraphRAG or remove dead code

5. **Notification system not wired**
   - Problem: Feature flag and settings exist, but no implementation; incidents resolved silently
   - Evidence: `FeatureFlagSettings.notification_delivery`, `NotificationService` missing
   - Recommendation: Implement Slack/email/PagerDuty integrations

6. **Scheduler module dormant**
   - Problem: `backend/scheduler/` exists but unused; no scheduled tasks
   - Evidence: Empty module; all orchestration is event-driven
   - Recommendation: Remove or implement if periodic tasks needed (e.g., incident archival)

7. **Limited evidence providers**
   - Problem: Only Prometheus/Azure Monitor (metrics), Elastic (logs), OTLP (traces); no CloudWatch, Datadog, Splunk out of the box
   - Evidence: Only 3 evidence providers implemented
   - Recommendation: Extend adapter factory with AWS CloudWatch, Datadog, Splunk adapters

### Medium

8. **Evaluation pipeline incomplete**
   - Problem: `EvaluationEngine` framework exists but evaluation strategies are incomplete
   - Evidence: `backend/evaluation/strategies/` has framework but limited implementations
   - Recommendation: Complete investigation evaluation strategies

9. **Missing observability gaps**
   - Problem: Not all critical phases have metrics (e.g., evidence collection latency per provider, LLM token usage)
   - Evidence: Some metrics exported, but not comprehensive
   - Recommendation: Add metrics for evidence collection, LLM latency, verification latency

10. **Testing coverage imbalances**
    - Problem: 800+ tests, but some modules under-tested (e.g., `backend/policies/`)
    - Evidence: Test files exist for core services, but not all
    - Recommendation: Increase coverage for policy engine, edge cases

11. **Documentation**
    - Problem: Limited inline documentation; architecture not documented
    - Evidence: This PROJECT_CONTEXT.md is first comprehensive docs
    - Recommendation: Add docstrings to all public functions; update README.md

---

## 23. AI Coding Agent Context

### Important Rules

1. **Do NOT break the closed-loop orchestration contract**
   - `ClosedLoopOrchestrator.run()` must always return `ClosedLoopResult` (never raise exceptions)
   - All phases (investigation, remediation, deployment, verification) must be tolerant of partial failures

2. **All I/O must be async**
   - No blocking calls anywhere in the codebase
   - Use `asyncio`, `httpx`, `asyncpg`, `redis-py` async APIs exclusively
   - Never use `requests`, `time.sleep()`, or synchronous database drivers

3. **Configuration is immutable after startup**
   - `AppSettings` loaded once at boot; not reloadable
   - Don't try to hot-reload configuration; restart the application

4. **Secrets are never logged**
   - All credentials must use `SecretStr` from Pydantic
   - Never log `api_key`, `password`, `token` values (use `repr()` which redacts)
   - KeyVault secrets fetched once at startup and cached

5. **Database uses ORM (SQLAlchemy), not raw SQL**
   - Write queries through `PostgreSQLIncidentRepository` or `BaseRepository`
   - Never construct raw SQL strings (SQL injection risk)

6. **LLM requests must be validated structurally**
   - All LLM responses must match the expected JSON schema
   - Strip hallucinated evidence IDs before storing

7. **Idempotency is critical**
   - All state-mutating operations must use idempotency keys
   - Deployment requests keyed by `(correlation_id, cycle)`; same key prevents duplicates

8. **Event-driven architecture**
   - Emit lifecycle events via `RedisWorkflowEventPublisher`
   - Don't use direct function calls for orchestration; use events

### Do Not Break

- **API Contract:** `/api/v1/incidents` POST/GET must remain compatible
- **Incident Model:** Fields like `incident_id`, `status`, `affected_services` are immutable after creation
- **Database Schema:** `incidents` table structure must not change without migration
- **LLM Provider Protocol:** All LLM providers must satisfy the `LLMProvider` protocol
- **Evidence Provider Protocol:** All evidence providers must satisfy the `MetricsEvidenceProvider`, `LogsEvidenceProvider`, `TracesEvidenceProvider` protocols
- **Configuration:** `AppSettings` class must remain loadable from environment variables
- **Health Checks:** `/health`, `/health/live`, `/health/ready` endpoints must exist

### Safe Modification Areas

- Adding new evidence providers: extend `backend/providers/evidence/` following `EvidenceHttpBase` pattern
- Adding new LLM providers: extend `backend/providers/llm/` following OpenAI-compatible pattern
- Adding new policies: extend `RemediationPolicyEngine` with new risk-level rules
- Adding new metrics/traces: extend OpenTelemetry instrumentation (non-breaking)
- Adding new tests: extend `backend/tests/` (no impact on production code)
- Improving logging/observability: add structlog bindings, OTel instrumentation (non-breaking)

### High-Risk Areas

1. **ClosedLoopOrchestrator.run()** — If modified, ensure it still gracefully handles all error scenarios
2. **Evidence collection** — If modified, ensure partial failures don't block investigation
3. **LLM integration** — If modified, ensure fallback chain still works
4. **Database operations** — If modified, ensure transactions are properly scoped
5. **Deployment pipeline** — If modified, ensure idempotency is preserved
6. **Configuration loading** — If modified, ensure secrets remain protected

### Common Dependencies

- `AppSettings` ← loaded at startup, injected everywhere
- `ApplicationContainer` ← manages all resource lifecycles
- `RedisConnection` ← used for caching, streams, locks
- `PostgreSQLIncidentRepository` ← used for incident CRUD
- `LLMProvider` ← used by investigation, remediation planning
- `MetricsEvidenceProvider`, `LogsEvidenceProvider`, `TracesEvidenceProvider` ← used by evidence collection
- `AzureKeyVault` ← used at startup for secret resolution

### Recommended Investigation Order (for New Feature/Bug)

1. **Understand the incident flow:** Read `ClosedLoopOrchestrator.run()`
2. **If API-related:** Check `backend/api/routers/incidents.py`
3. **If investigation-related:** Check `InvestigationOrchestrator.investigate()`, `LLMInvestigationAgent`
4. **If deployment-related:** Check `DeploymentPipeline`, `GitHubActionsDeploymentProvider`
5. **If verification-related:** Check `VerificationCoordinator`, `AzureMonitorMetricsSnapshot`
6. **If configuration-related:** Check `backend/configuration/settings.py`
7. **If evidence-related:** Check `EvidenceOrchestrator`, evidence provider implementations
8. **If LLM-related:** Check `backend/providers/llm/`, `LLMInvestigationAgent`
9. **If database-related:** Check `backend/db/`, `PostgreSQLIncidentRepository`
10. **If async/performance-related:** Check for blocking calls, connection pooling, retry logic

---

## 24. Summary

**Sentinel AI Platform** is a production-grade autonomous incident response system built on async-first Python (FastAPI + PostgreSQL + Redis + LLM agents). The core innovation is the **closed-loop orchestration** pattern (investigation → remediation → deployment → verification) that enables fully autonomous incident resolution without human intervention.

**Strengths:**
- Well-architected, incident-type agnostic orchestration
- Multiple evidence sources with graceful degradation
- AI-backed investigation with LLM providers
- Strong observability (OpenTelemetry, structured logging)
- Production-ready infrastructure (Azure Container Apps, KeyVault, Managed Identity)
- Comprehensive test suite (800+ tests)

**Weaknesses:**
- No frontend (API-only)
- Approval workflow gated but not tracked
- Limited evidence provider ecosystem (no CloudWatch, Datadog, Splunk out of box)
- GraphRAG feature-flagged off (dead code)
- Scheduler module unused
- Notification system not implemented

**Status:** Production-ready for core incident lifecycle; features like GraphRAG, evaluation pipelines, and notifications are incomplete but don't block core functionality.

---

**END OF PROJECT_CONTEXT.md**

Generated: October 2026  
Repository: sentinel-ai-platform v0.1.0  
Tests: 800 passing, 1 warning (unrelated Starlette deprecation)  
Type Safety: mypy strict mode passing  
Linting: ruff passing
