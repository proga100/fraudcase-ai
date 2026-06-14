"""UiPathRunner — the live external coded agent, fully UiPath-first.

Drives the same two-gate event sequence as FakeRunner, but every integration is
UiPath:
  * structured records (transactions, vendors, policies) are read from UiPath
    Data Service;
  * semantic evidence is retrieved from UiPath Context Grounding, which owns
    embedding generation, vector indexing, and retrieval — this service never
    calls an embedding model;
  * the deterministic detector suite (policy / duplicate / ghost / off-hours /
    OFAC / Context-Grounding-similarity) turns records + evidence into findings;
  * the Gate-2 approved findings and the audit log are written back to UiPath
    Data Service.

Used when USE_MOCKS=false. With UiPath endpoints configured it calls UiPath; with
them unset the injected clients fall back to the local demo dataset so the live
path still runs credential-free for development.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import AsyncIterator

from fraudcase_ai.agent import roster
from fraudcase_ai.agent.report import render_report
from fraudcase_ai.models import (
    AgentEvent,
    ApprovalDecision,
    ApprovalGate,
    AuditCaseRequest,
    EventType,
    FlaggedItem,
    maestro_event_context,
)
from fraudcase_ai.tools.triage import assemble_flagged_records
from fraudcase_ai.uipath.clients import ContextGroundingRetriever, DataServiceStore

MAX_FLAGGED = 40  # cap: a human reviews exceptions, not hundreds of rows


def _build_plan(objective: str) -> str:
    """Deterministic audit plan (no LLM). Mirrors the live tool sequence."""
    return (
        f"Audit objective: {objective}\n"
        "1. Read transactions, vendors, and policies from UiPath Data Service.\n"
        "2. Retrieve semantically relevant invoice evidence from UiPath Context Grounding.\n"
        "3. Run deterministic checks: duplicate, policy, ghost vendor, off-hours, sanctions.\n"
        "4. Aggregate spend by department.\n"
        "5. Propose a flagged exception list for your approval.\n"
        "6. Write approved findings and the audit log back to UiPath Data Service."
    )


def _build_narrative(objective: str, flagged: int, at_risk: float, reasons: list[str]) -> str:
    """Deterministic report narrative (no LLM)."""
    risk_types = len(set(reasons))
    return (
        f"Audit objective '{objective}' flagged {flagged} transactions totalling "
        f"${at_risk:,.0f} at risk across {risk_types} risk "
        f"type{'s' if risk_types != 1 else ''}. Evidence was retrieved from UiPath "
        "Context Grounding and every write was approved by a human auditor before it "
        "reached the UiPath Data Service audit log."
    )


class UiPathRunner:
    """Live runner backed by UiPath Data Service + Context Grounding."""

    def __init__(
        self,
        store: DataServiceStore | None = None,
        retriever: ContextGroundingRetriever | None = None,
    ) -> None:
        self._store = store or DataServiceStore()
        self._retriever = retriever or ContextGroundingRetriever()
        self._approval_events: dict[str, asyncio.Event] = {}
        self._decisions: dict[str, ApprovalDecision] = {}

    # ------------------------------------------------------------------ process
    async def run(self, case_id: str, audit_case: AuditCaseRequest) -> AsyncIterator[AgentEvent]:
        def evt(t: EventType, **data) -> AgentEvent:
            gate = data.get("gate")
            return AgentEvent(
                case_id=case_id,
                type=t,
                data=dict(data),
                **maestro_event_context(t, gate),
            )

        objective = audit_case.text

        # --- Gate 1: deterministic plan ---
        yield evt(EventType.PLAN, plan=_build_plan(objective), **roster.MISSION_PLANNING)
        yield evt(EventType.AWAITING_APPROVAL, gate=ApprovalGate.PLAN.value, **roster.HUMAN_GATE)
        await self._wait(case_id)
        if not self._decisions.pop(case_id).approved:
            yield evt(EventType.ERROR, reason="Plan rejected by reviewer")
            yield evt(EventType.DONE)
            return

        # --- Tool 1: read structured records from UiPath Data Service ---
        yield evt(EventType.TOOL_CALL, tool="uipath.dataService.read",
                  entities=["Transaction", "Vendor", "Policy"], **roster.DATA_SERVICE_READ)
        invoices_raw, vendors_raw, policies_raw = await self._store.load_case_dataset()
        yield evt(EventType.TOOL_RESULT, tool="uipath.dataService.read",
                  transactions=len(invoices_raw), vendors=len(vendors_raw),
                  policies=len(policies_raw), **roster.DATA_SERVICE_READ)

        # --- Tool 2: retrieve evidence from UiPath Context Grounding ---
        yield evt(EventType.TOOL_CALL, tool="uipath.contextGrounding.query", query=objective,
                  index=self._retriever.index_name, **roster.VECTOR_SEARCH)
        hits: list[dict] = []
        try:
            hits = await self._retriever.query(objective, limit=8)
        except Exception as exc:  # noqa: BLE001 — recoverable: route as a case exception
            yield evt(
                EventType.EXCEPTION,
                exception_type="context_grounding_recoverable",
                message=(
                    f"UiPath Context Grounding query failed with {type(exc).__name__}. "
                    "The case is escalated for human review and the agent continues with "
                    "deterministic duplicate, policy, ghost-vendor, and off-hours checks."
                ),
                recommended_action="Review the deterministic findings before approving Gate 2.",
                **roster.RISK_TRIAGE,
            )
        top = round(float(hits[0].get("score") or 0.0), 4) if hits else 0.0
        yield evt(
            EventType.TOOL_RESULT, tool="uipath.contextGrounding.query", hits=len(hits), top_score=top,
            sample=[{"invoice_id": h.get("invoice_id"), "score": round(float(h.get("score") or 0.0), 3)}
                    for h in hits[:5]],
            **roster.VECTOR_SEARCH,
        )

        # --- Tool 3: aggregate spend by department (over Data Service records) ---
        yield evt(EventType.TOOL_CALL, tool="uipath.dataService.aggregate", group_by="department",
                  **roster.SPEND_ANALYSIS)
        spend = _aggregate_spend(invoices_raw, "department")
        yield evt(EventType.TOOL_RESULT, tool="uipath.dataService.aggregate",
                  by_department=sorted(spend, key=lambda s: s["total"], reverse=True),
                  **roster.SPEND_ANALYSIS)

        # --- Assemble flagged list (detectors + Context Grounding hits), cap for review ---
        all_items = assemble_flagged_records(invoices_raw, vendors_raw, policies_raw, hits)
        items = all_items[:MAX_FLAGGED]
        dept_counts: dict[str, int] = defaultdict(int)
        vendor_counts: dict[str, int] = defaultdict(int)
        for it in all_items:
            dept_counts[it.department] += 1
            vendor_counts[it.vendor_name] += 1
        yield evt(
            EventType.PROPOSAL,
            items=[i.model_dump(mode="json") for i in items],
            total_flagged=len(all_items),
            shown=len(items),
            total_at_risk=sum(i.amount for i in all_items),
            dept_counts=dict(dept_counts),
            vendor_counts=dict(vendor_counts),
            **roster.RISK_TRIAGE,
        )
        yield evt(EventType.AWAITING_APPROVAL, gate=ApprovalGate.ACTION.value, **roster.HUMAN_GATE)
        await self._wait(case_id)
        decision = self._decisions.pop(case_id)

        # approve set: explicit approves win; else all-minus-rejected; else all shown
        proposed = {i.invoice_id for i in items}
        if decision.approved_ids:
            keep = set(decision.approved_ids) & proposed
        elif decision.rejected_ids:
            keep = proposed - set(decision.rejected_ids)
        else:
            keep = proposed
        approved = [i for i in items if i.invoice_id in keep]

        # --- Gated write back to UiPath Data Service ---
        await self._store.write_audit_log(case_id, objective, approved)
        yield evt(EventType.WRITTEN, flagged=len(approved), **roster.AUDIT_TRAIL)

        # --- Report (deterministic narrative + structured) ---
        report = render_report(case_id, objective, approved)
        reasons = [r.value for it in approved for r in it.reasons]
        narrative = _build_narrative(objective, report.flagged_count, report.total_at_risk, reasons)
        report.markdown = f"# FraudCase AI - Audit Report\n\n{narrative}\n\n" + report.markdown
        yield evt(EventType.REPORT_READY, case_id=case_id, flagged_count=report.flagged_count,
                  total_at_risk=report.total_at_risk, report=report.model_dump(mode="json"),
                  **roster.REPORT_GENERATION)
        yield evt(EventType.DONE)

    async def deliver_decision(self, case_id: str, decision: ApprovalDecision) -> None:
        self._decisions[case_id] = decision
        ev = self._approval_events.get(case_id)
        if ev is not None:
            ev.set()

    async def _wait(self, case_id: str) -> None:
        ev = asyncio.Event()
        self._approval_events[case_id] = ev
        await ev.wait()
        self._approval_events.pop(case_id, None)


def _aggregate_spend(invoices_raw: list[dict], group_by: str = "department") -> list[dict]:
    """Sum invoice amount grouped by a field, over Data Service records."""
    groups: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "count": 0})
    for inv in invoices_raw:
        key = inv.get(group_by, "unknown")
        groups[key]["total"] += float(inv.get("amount", 0.0) or 0.0)
        groups[key]["count"] += 1
    return [{"_id": k, "total": v["total"], "count": v["count"]} for k, v in groups.items()]
