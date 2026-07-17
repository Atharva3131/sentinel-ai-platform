"""SRE workflow event-name constants.

All names follow the ``Domain.Subject.PastTense`` convention defined in
docs/05_EVENTS.md.  Use these constants everywhere events are emitted so that
consumers can subscribe by exact string without typos.
"""

from __future__ import annotations

# Workflow lifecycle
SRE_WORKFLOW_STARTED = "SRE.Workflow.Started"
SRE_WORKFLOW_COMPLETED = "SRE.Workflow.Completed"
SRE_WORKFLOW_FAILED = "SRE.Workflow.Failed"
SRE_WORKFLOW_CANCELLED = "SRE.Workflow.Cancelled"

# Retrieval
SRE_CONTEXT_RETRIEVAL_STARTED = "SRE.Context.RetrievalStarted"
SRE_CONTEXT_RETRIEVED = "SRE.Context.Retrieved"

# Planning
SRE_PLAN_BUILD_STARTED = "SRE.Plan.BuildStarted"
SRE_PLAN_BUILT = "SRE.Plan.Built"

# Analysis
SRE_ANALYSIS_STARTED = "SRE.Analysis.Started"
SRE_ANALYSIS_COMPLETED = "SRE.Analysis.Completed"

# Confidence evaluation
SRE_CONFIDENCE_EVALUATION_STARTED = "SRE.Confidence.EvaluationStarted"
SRE_CONFIDENCE_EVALUATED = "SRE.Confidence.Evaluated"

# Approval
SRE_APPROVAL_REQUESTED = "SRE.Approval.Requested"
SRE_APPROVAL_RECEIVED = "SRE.Approval.Received"

# Recovery
SRE_RECOVERY_STARTED = "SRE.Recovery.Started"
SRE_RECOVERY_COMPLETED = "SRE.Recovery.Completed"

# Audit
SRE_AUDIT_RECORDED = "SRE.Audit.Recorded"
