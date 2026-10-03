"""Incident lifecycle event-name constants.

All names follow ``Domain.Subject.PastTense`` convention so consumers can
subscribe by exact string without typos.  These constants are the canonical
source of truth for incident orchestration events across the platform.
"""

from __future__ import annotations

# ── Ingestion ────────────────────────────────────────────────────────────────

INCIDENT_CREATED = "Incident.Lifecycle.Created"
"""An incident was accepted, normalised, and persisted."""

INCIDENT_DUPLICATE_DETECTED = "Incident.Lifecycle.DuplicateDetected"
"""Incoming signal matched an already-open incident; de-duplicated."""

# ── Investigation ────────────────────────────────────────────────────────────

INVESTIGATION_STARTED = "Incident.Investigation.Started"
"""An investigation workflow was launched for the incident."""

INVESTIGATION_ITERATION_STARTED = "Incident.Investigation.IterationStarted"
"""A new iteration of the iterative investigation loop was entered."""

INVESTIGATION_ITERATION_COMPLETED = "Incident.Investigation.IterationCompleted"
"""An investigation iteration completed (may continue if confidence low)."""

# ── Evidence collection ──────────────────────────────────────────────────────

EVIDENCE_COLLECTION_STARTED = "Incident.Evidence.CollectionStarted"
"""Evidence collection from all registered providers has begun."""

EVIDENCE_COLLECTED = "Incident.Evidence.Collected"
"""Evidence collection completed successfully with all providers responding."""

EVIDENCE_COLLECTION_PARTIAL_FAILURE = "Incident.Evidence.CollectionPartialFailure"
"""Evidence collection completed but one or more providers failed."""

EVIDENCE_ADDITIONAL_REQUESTED = "Incident.Evidence.AdditionalRequested"
"""Investigation agent requested additional evidence categories."""

# ── Hypothesis / RCA ────────────────────────────────────────────────────────

HYPOTHESES_GENERATED = "Incident.Hypothesis.Generated"
"""A candidate set of hypotheses was generated from collected evidence."""

ROOT_CAUSE_IDENTIFIED = "Incident.RCA.RootCauseIdentified"
"""A root cause was identified with confidence >= acceptance threshold."""

RCA_INCONCLUSIVE = "Incident.RCA.Inconclusive"
"""RCA completed but could not identify a root cause above the threshold."""

# ── Remediation ──────────────────────────────────────────────────────────────

REMEDIATION_PLANNED = "Incident.Remediation.Planned"
"""A remediation plan was generated from the RCA."""

POLICY_EVALUATED = "Incident.Remediation.PolicyEvaluated"
"""Remediation actions were evaluated against the policy engine."""

REMEDIATION_STARTED = "Incident.Remediation.Started"
"""Remediation plan execution began."""

REMEDIATION_COMPLETED = "Incident.Remediation.Completed"
"""Remediation plan execution finished (may have partial failures)."""

# ── Validation / Verification ────────────────────────────────────────────────

VALIDATION_STARTED = "Incident.Validation.Started"
"""Post-remediation validation checks began."""

VALIDATION_COMPLETED = "Incident.Validation.Completed"
"""Post-remediation validation checks finished."""

VERIFICATION_STARTED = "Incident.Verification.Started"
"""Before/after metric comparison verification began."""

VERIFICATION_COMPLETED = "Incident.Verification.Completed"
"""Before/after metric comparison verification finished."""

# ── Resolution ───────────────────────────────────────────────────────────────

INCIDENT_RESOLVED = "Incident.Lifecycle.Resolved"
"""The incident was fully resolved and verified."""

INCIDENT_REINVESTIGATION_REQUIRED = "Incident.Lifecycle.ReinvestigationRequired"
"""Validation or verification failed; reinvestigation was triggered."""

INCIDENT_FAILED = "Incident.Lifecycle.Failed"
"""The orchestration pipeline encountered an unrecoverable failure."""

INCIDENT_CANCELLED = "Incident.Lifecycle.Cancelled"
"""Investigation was explicitly cancelled (timeout or operator action)."""

INCIDENT_ESCALATED = "Incident.Lifecycle.Escalated"
"""Incident was escalated beyond autonomous capability."""

# ── Post-deployment verification ─────────────────────────────────────────────

VERIFICATION_OBSERVATION_STARTED = "Incident.Verification.ObservationStarted"
"""Post-deployment observation window began."""

VERIFICATION_EVIDENCE_COLLECTED = "Incident.Verification.EvidenceCollected"
"""Post-deployment evidence was collected for comparison."""

VERIFICATION_PASSED = "Incident.Verification.Passed"
"""Before/after verification comparison passed — incident improving."""

VERIFICATION_FAILED = "Incident.Verification.Failed"
"""Before/after verification comparison failed — incident not improving."""

VERIFICATION_TIMED_OUT = "Incident.Verification.TimedOut"
"""Verification observation window exceeded its budget."""

REINVESTIGATION_STARTED = "Incident.Reinvestigation.Started"
"""A reinvestigation cycle was triggered after verification failure."""

REINVESTIGATION_LIMIT_REACHED = "Incident.Reinvestigation.LimitReached"
"""Maximum reinvestigation cycles reached; incident escalated."""
