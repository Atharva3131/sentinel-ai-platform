# Codex Project Instructions

You are the implementation engineer for this repository.

## Project

This project is a production-grade Autonomous AI Operations Platform.

It is NOT a chatbot.

It is an event-driven backend platform that orchestrates autonomous AI workflows using LangGraph, asynchronous Python, distributed systems, GraphRAG, observability, and self-healing mechanisms.

Always prioritize production engineering practices over quick implementation.

---

# Core Technologies

- Python 3.12
- FastAPI
- asyncio
- LangGraph
- LangChain
- Redis
- PostgreSQL
- Neo4j
- Azure Cosmos DB
- Azure Blob Storage
- Docker
- SQLAlchemy
- Alembic
- OpenTelemetry
- Prometheus
- Grafana
- Phoenix
- LangSmith

---

# Architecture Principles

- Event-driven architecture
- Async-first
- Modular design
- SOLID principles
- Dependency Injection
- Clean Architecture
- Repository Pattern
- Service Layer Pattern
- Stateless services
- Idempotent workflows

---

# Coding Standards

Always

- use type hints
- use Pydantic v2
- use async APIs whenever possible
- write structured logs
- create reusable services
- write production-quality code
- create tests for business logic
- keep functions focused
- prefer composition over inheritance

Never

- write blocking code inside async functions
- create global mutable state
- tightly couple modules
- place business logic inside API routes

---

# Logging

Every major operation must emit structured logs.

Every request should have

- correlation id
- workflow id
- execution id

---

# Error Handling

Implement

- retries
- exponential backoff
- circuit breakers
- graceful degradation
- meaningful exceptions

---

# Documentation

Every public class and function should contain concise documentation.

---

# Goal

Write code that would be acceptable in a production engineering team.