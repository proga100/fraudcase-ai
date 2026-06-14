"""In-memory audit case store for FraudCase AI.

Each audit case has:
  - metadata (case_id, case objective, status)
  - an asyncio.Queue[AgentEvent | None] — None is the sentinel for "stream done"
  - an optional final AuditReport (set when REPORT_READY fires)
  - a reference to the runner so approvals can be forwarded
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from fraudcase_ai.models import (
    AgentEvent,
    ApprovalDecision,
    AuditReport,
    AuditCaseRequest,
    ApprovalGate,
    EventType,
    MaestroCaseStage,
)
from fraudcase_ai.server.runner import AgentRunner


class AuditCaseRecord:
    def __init__(self, case_id: str, audit_case: AuditCaseRequest, runner: AgentRunner) -> None:
        self.case_id = case_id
        self.audit_case = audit_case
        self.runner = runner
        self.queue: asyncio.Queue[Optional[AgentEvent]] = asyncio.Queue()
        self.report: Optional[AuditReport] = None
        self.stage: MaestroCaseStage = MaestroCaseStage.CASE_INTAKE
        self.pending_gate: Optional[ApprovalGate] = None
        self.exception_count: int = 0


class AuditCaseStore:
    """Thread-safe (asyncio-safe) in-memory store of active/completed audit cases."""

    def __init__(self) -> None:
        self._cases: dict[str, AuditCaseRecord] = {}

    def create_case(self, audit_case: AuditCaseRequest, runner: AgentRunner) -> AuditCaseRecord:
        """Create a new audit case record and return it."""
        case_id = str(uuid.uuid4())
        record = AuditCaseRecord(case_id=case_id, audit_case=audit_case, runner=runner)
        self._cases[case_id] = record
        return record

    def get_case(self, case_id: str) -> Optional[AuditCaseRecord]:
        """Return the audit case record or None if unknown."""
        return self._cases.get(case_id)

    def require_case(self, case_id: str) -> AuditCaseRecord:
        """Return the audit case record or raise KeyError if unknown."""
        record = self._cases.get(case_id)
        if record is None:
            raise KeyError(case_id)
        return record

    async def push_approval(
        self, case_id: str, decision: ApprovalDecision
    ) -> None:
        """Forward an approval decision to the runner for this audit case."""
        record = self.require_case(case_id)
        record.pending_gate = None
        await record.runner.deliver_decision(case_id, decision)

    def set_report(self, case_id: str, report: AuditReport) -> None:
        """Persist the final audit report for a completed audit case."""
        record = self._cases.get(case_id)
        if record is not None:
            record.report = report
            record.stage = MaestroCaseStage.AUDIT_REPORT_READY

    def get_report(self, case_id: str) -> Optional[AuditReport]:
        record = self._cases.get(case_id)
        if record is None:
            return None
        return record.report

    def newest_report(self) -> Optional[AuditReport]:
        for record in reversed(list(self._cases.values())):
            if record.report is not None:
                return record.report
        return None

    def apply_event(self, event: AgentEvent) -> None:
        """Update case status from a streamed event."""
        record = self._cases.get(event.case_id)
        if record is None:
            return
        record.stage = event.maestro_stage
        if event.type == EventType.AWAITING_APPROVAL:
            gate = event.data.get("gate")
            record.pending_gate = ApprovalGate(gate) if gate in {g.value for g in ApprovalGate} else None
        if event.type == EventType.PROPOSAL:
            items = event.data.get("items") or []
            record.exception_count = int(event.data.get("total_flagged") or len(items))
        if event.type in {EventType.REPORT_READY, EventType.DONE}:
            record.pending_gate = None
        if event.type == EventType.DONE:
            record.stage = MaestroCaseStage.CLOSED

    def case_summary(self, case_id: str) -> dict:
        record = self.require_case(case_id)
        return {
            "case_id": record.case_id,
            "case_objective": record.audit_case.text,
            "maestro_stage": record.stage.value,
            "pending_gate": record.pending_gate.value if record.pending_gate else None,
            "exception_count": record.exception_count,
            "report_ready": record.report is not None,
        }


# Module-level singleton used by the FastAPI app.
audit_case_store = AuditCaseStore()
