"""Shared data contracts for FraudCase AI.

This module is the single source of truth. Every build slice (tools, agent, server,
frontend, data loader) codes against these types. Do not fork or redefine them in a
slice — import from here. Changes here are coordinated by the orchestrator.

Field names on Invoice/Vendor/Policy intentionally match the JSON emitted by
demo_dataset/generate_data.py so loading is a straight parse.
"""

from __future__ import annotations

from datetime import datetime, date, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Domain records (mirror generate_data.py output)
# --------------------------------------------------------------------------- #
class Vendor(BaseModel):
    vendor_id: str
    vendor_name: str
    country: str
    category: str
    onboarded: date
    is_ghost: bool = False
    risk_score: float = 0.0


class Policy(BaseModel):
    rule_id: str
    category: str  # "*" means applies to all categories
    max_amount: float
    text: str


class Invoice(BaseModel):
    invoice_id: str
    vendor_id: str
    vendor_name: str
    department: str
    category: str
    amount: float
    currency: str = "USD"
    payment_method: str
    invoice_date: date
    payment_hour: int
    approved_by: str
    notes: str
    # injected-fraud ground-truth flags (used for eval + demo narration)
    is_duplicate: bool = False
    is_near_duplicate: bool = False
    is_policy_violation: bool = False
    is_off_hours: bool = False
    is_ghost_vendor: bool = False
    is_fraud_exemplar: bool = False
    fraud_label: int = 0
    # retrieval — embeddings + vector indexing are owned by UiPath Context Grounding;
    # these fields only carry the local/demo dataset text used by the offline fallback.
    embedding_text: str = ""
    embedding: Optional[list[float]] = None


# --------------------------------------------------------------------------- #
# Audit findings
# --------------------------------------------------------------------------- #
class FlaggedReason(str, Enum):
    DUPLICATE = "duplicate"
    NEAR_DUPLICATE = "near_duplicate"
    GHOST_VENDOR = "ghost_vendor"
    POLICY_VIOLATION = "policy_violation"
    OFF_HOURS = "off_hours"
    OFAC_HIT = "ofac_hit"
    VECTOR_SIMILAR = "vector_similar"  # semantically close to a known fraud exemplar


class FlaggedItem(BaseModel):
    """One suspicious invoice the agent proposes flagging. Shown in Gate 2."""

    invoice_id: str
    vendor_name: str
    department: str
    amount: float
    reasons: list[FlaggedReason] = Field(default_factory=list)
    similarity: Optional[float] = None  # vector score if VECTOR_SIMILAR
    detail: str = ""  # human-readable one-liner for the UI row


# --------------------------------------------------------------------------- #
# Human-in-the-loop approval
# --------------------------------------------------------------------------- #
class ApprovalGate(str, Enum):
    PLAN = "plan"      # Gate 1: approve/edit the plan before any tool runs
    ACTION = "action"  # Gate 2: per-item approve before the write


class ApprovalDecision(BaseModel):
    """Posted by the UI to resume a paused audit case."""

    gate: ApprovalGate
    approved: bool = True               # PLAN gate: approve/reject the whole plan
    edited_plan: Optional[str] = None   # PLAN gate: user-edited plan text
    approved_ids: list[str] = Field(default_factory=list)   # ACTION gate
    rejected_ids: list[str] = Field(default_factory=list)   # ACTION gate


# --------------------------------------------------------------------------- #
# Streaming event protocol (agent -> server -> UI over SSE)
# --------------------------------------------------------------------------- #
class MaestroCaseStage(str, Enum):
    CASE_INTAKE = "case_intake"
    AUDIT_PLAN_REVIEW = "audit_plan_review"
    AGENT_INVESTIGATION = "agent_investigation"
    EXCEPTION_REVIEW = "exception_review"
    AUDIT_LOG_WRITE = "audit_log_write"
    AUDIT_REPORT_READY = "audit_report_ready"
    CLOSED = "closed"


class ActorType(str, Enum):
    HUMAN = "human"
    EXTERNAL_AI_AGENT = "external_ai_agent"
    SERVICE_TASK = "service_task"
    SYSTEM = "system"


class EventType(str, Enum):
    PLAN = "plan"                      # agent proposed a plan (triggers Gate 1)
    TOOL_CALL = "tool_call"            # agent invoked a tool
    TOOL_RESULT = "tool_result"       # tool returned (carry similarity scores, counts)
    EXCEPTION = "exception"           # recoverable case exception surfaced to a human
    PROPOSAL = "proposal"             # flagged list assembled (triggers Gate 2)
    AWAITING_APPROVAL = "awaiting_approval"  # audit case paused, which gate
    WRITTEN = "written"               # mark_flagged committed
    REPORT_READY = "report_ready"     # audit report available
    ERROR = "error"
    DONE = "done"


class AgentEvent(BaseModel):
    """One item in the SSE stream. `data` shape depends on `type`."""

    case_id: str
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)
    maestro_stage: MaestroCaseStage = MaestroCaseStage.CASE_INTAKE
    actor_type: ActorType = ActorType.SYSTEM
    ts: datetime = Field(default_factory=_utcnow)


def maestro_event_context(
    event_type: EventType,
    gate: ApprovalGate | str | None = None,
) -> dict[str, MaestroCaseStage | ActorType]:
    """Return UiPath Maestro Case stage and actor metadata for an event."""
    gate_value = gate.value if isinstance(gate, ApprovalGate) else gate
    if event_type == EventType.PLAN:
        return {
            "maestro_stage": MaestroCaseStage.AUDIT_PLAN_REVIEW,
            "actor_type": ActorType.EXTERNAL_AI_AGENT,
        }
    if event_type == EventType.AWAITING_APPROVAL:
        return {
            "maestro_stage": (
                MaestroCaseStage.AUDIT_PLAN_REVIEW
                if gate_value == ApprovalGate.PLAN.value
                else MaestroCaseStage.EXCEPTION_REVIEW
            ),
            "actor_type": ActorType.HUMAN,
        }
    if event_type in {EventType.TOOL_CALL, EventType.TOOL_RESULT}:
        return {
            "maestro_stage": MaestroCaseStage.AGENT_INVESTIGATION,
            "actor_type": ActorType.EXTERNAL_AI_AGENT,
        }
    if event_type in {EventType.EXCEPTION, EventType.PROPOSAL}:
        return {
            "maestro_stage": MaestroCaseStage.EXCEPTION_REVIEW,
            "actor_type": ActorType.EXTERNAL_AI_AGENT,
        }
    if event_type == EventType.WRITTEN:
        return {
            "maestro_stage": MaestroCaseStage.AUDIT_LOG_WRITE,
            "actor_type": ActorType.SERVICE_TASK,
        }
    if event_type == EventType.REPORT_READY:
        return {
            "maestro_stage": MaestroCaseStage.AUDIT_REPORT_READY,
            "actor_type": ActorType.SERVICE_TASK,
        }
    if event_type == EventType.DONE:
        return {
            "maestro_stage": MaestroCaseStage.CLOSED,
            "actor_type": ActorType.SYSTEM,
        }
    return {
        "maestro_stage": MaestroCaseStage.CASE_INTAKE,
        "actor_type": ActorType.SYSTEM,
    }


# --------------------------------------------------------------------------- #
# Final artifact
# --------------------------------------------------------------------------- #
class AuditReport(BaseModel):
    case_id: str
    case_objective: str
    generated_at: datetime = Field(default_factory=_utcnow)
    flagged_count: int = 0
    total_at_risk: float = 0.0
    items: list[FlaggedItem] = Field(default_factory=list)
    markdown: str = ""


# --------------------------------------------------------------------------- #
# Audit case lifecycle
# --------------------------------------------------------------------------- #
class AuditCaseRequest(BaseModel):
    text: str  # plain-English audit case objective, e.g. "Audit this month's vendor payments"


class AuditCaseStarted(BaseModel):
    case_id: str


# --------------------------------------------------------------------------- #
# AI Audit Assistant (the "Ask Audit Agent" drawer)
# --------------------------------------------------------------------------- #
class AskRequest(BaseModel):
    """A question for the AI Audit Assistant, optionally scoped to an audit case."""

    question: str = Field(min_length=1, max_length=2000)
    case_id: Optional[str] = None
    invoice_context: Optional[dict[str, Any]] = None


class AskResponse(BaseModel):
    answer: str
    model: str
    ai_generated: bool = True  # the UI must surface this — answers need human review


CaseStatus = Literal["planning", "awaiting_plan", "executing", "awaiting_action", "writing", "done", "error"]
