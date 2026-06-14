"""RealRunner — the live agent: real Gemini reasoning + real Atlas vector search.

Drives the same two-gate event sequence as FakeRunner, but:
  * PLAN + report narrative come from Gemini 3 (Vertex AI)
  * semantic similarity uses real Atlas $vectorSearch (the MongoDB superpower)
  * fast O(n) detectors find duplicates / policy / ghost / off-hours
  * the gated write hits real Atlas

Used when USE_MOCKS=false. Requires ATLAS_URI + GCP creds in the environment.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import AsyncIterator

from pymongo import MongoClient

from fraudcase_ai.agent import llm, roster
from fraudcase_ai.agent.embedding import embed_query
from fraudcase_ai.agent.mcp_reads import mcp_aggregate
from fraudcase_ai.agent.report import render_report
from fraudcase_ai.config import get_settings
from fraudcase_ai.models import (
    AgentEvent,
    ApprovalDecision,
    ApprovalGate,
    EventType,
    FlaggedItem,
    AuditCaseRequest,
    maestro_event_context,
)
from fraudcase_ai.tools.mongo_reads import aggregate_spend, vector_search_transactions
from fraudcase_ai.tools.triage import assemble_flagged

MAX_FLAGGED = 40  # cap: a human reviews exceptions, not hundreds of rows


class RealRunner:
    """Live runner backed by Atlas + Vertex AI."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = MongoClient(self._settings.atlas_uri)
        self._db = self._client[self._settings.db_name]
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

        db = self._db

        # --- Gate 1: Gemini-authored plan (timeout-guarded so it can't hang) ---
        _PLAN_FALLBACK = (
            "1. Vector-search transactions similar to known fraud patterns.\n"
            "2. Aggregate spend by department.\n"
            "3. Check policy limits, duplicates, ghost vendors, off-hours.\n"
            "4. Screen payees against sanctions.\n"
            "5. Propose a flagged list for your approval."
        )
        try:
            plan = await asyncio.wait_for(asyncio.to_thread(llm.plan_for, audit_case.text), timeout=30)
        except (asyncio.TimeoutError, Exception):
            plan = _PLAN_FALLBACK
        yield evt(EventType.PLAN, plan=plan, **roster.MISSION_PLANNING)
        yield evt(EventType.AWAITING_APPROVAL, gate=ApprovalGate.PLAN.value, **roster.HUMAN_GATE)
        await self._wait(case_id)
        if not self._decisions.pop(case_id).approved:
            yield evt(EventType.ERROR, reason="Plan rejected by reviewer")
            yield evt(EventType.DONE)
            return

        # --- Tool 1: $vectorSearch via the MongoDB MCP server (partner integration) ---
        yield evt(EventType.TOOL_CALL, tool="mongodb.vectorSearch", query=audit_case.text,
                  via="MongoDB MCP server", **roster.VECTOR_SEARCH)
        qvec = await asyncio.to_thread(embed_query, audit_case.text)
        source = "MongoDB MCP server"
        hits: list[dict] = []
        vector_exception: str | None = None
        if self._settings.use_mcp_reads:
            pipeline = [
                {"$vectorSearch": {"index": self._settings.vector_index_name, "path": "embedding",
                                   "queryVector": qvec, "numCandidates": 150, "limit": 8}},
                {"$addFields": {"score": {"$meta": "vectorSearchScore"}}},
                {"$project": {"_id": 0, "embedding": 0}},
            ]
            try:
                hits = await mcp_aggregate(self._settings.db_name, self._settings.txn_collection, pipeline)
            except Exception as exc:  # noqa: BLE001
                vector_exception = type(exc).__name__
                hits = []
        if not hits:  # fallback keeps the demo reliable if the MCP subprocess hiccups
            if vector_exception:
                yield evt(
                    EventType.EXCEPTION,
                    exception_type="mongodb_mcp_fallback",
                    message=f"MongoDB MCP vector search failed with {vector_exception}; falling back to direct Atlas driver.",
                    recommended_action="Review fallback evidence before approving Gate 2 findings.",
                    **roster.RISK_TRIAGE,
                )
            hits = await asyncio.to_thread(vector_search_transactions, db, qvec, 8)
            source = "direct driver (MCP fallback)"
        top = round(hits[0]["score"], 4) if hits else 0.0
        yield evt(
            EventType.TOOL_RESULT, tool="mongodb.vectorSearch", via=source, hits=len(hits), top_score=top,
            sample=[{"invoice_id": h.get("invoice_id"), "vendor_name": h.get("vendor_name"),
                     "score": round(h["score"], 3)} for h in hits[:5]],
            **roster.VECTOR_SEARCH,
        )

        # --- Tool 2: real aggregation ---
        yield evt(EventType.TOOL_CALL, tool="mongodb.aggregate", group_by="department", **roster.SPEND_ANALYSIS)
        spend = await asyncio.to_thread(aggregate_spend, db, "department")
        yield evt(EventType.TOOL_RESULT, tool="mongodb.aggregate",
                  by_department=sorted(spend, key=lambda s: s["total"], reverse=True),
                  **roster.SPEND_ANALYSIS)

        # --- Assemble flagged list (fast detectors + vector hits), cap for review ---
        all_items = await asyncio.to_thread(self._assemble, db, hits)
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

        # --- Gated write to Atlas ---
        from fraudcase_ai.tools.flagging import mark_flagged

        await asyncio.to_thread(mark_flagged, db, case_id, [i.invoice_id for i in approved], approved)
        yield evt(EventType.WRITTEN, flagged=len(approved), **roster.AUDIT_TRAIL)

        # --- Report (Gemini narrative + structured) ---
        report = render_report(case_id, audit_case.text, approved)
        reasons = [r.value for it in approved for r in it.reasons]
        try:
            narrative = await asyncio.wait_for(
                asyncio.to_thread(llm.summarize, audit_case.text, report.flagged_count, report.total_at_risk, reasons),
                timeout=30,
            )
        except (asyncio.TimeoutError, Exception):
            narrative = (f"Flagged {report.flagged_count} transactions totalling "
                         f"${report.total_at_risk:,.0f} at risk across {len(set(reasons))} risk types.")
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

    # --------------------------------------------------------------- audit
    def _assemble(self, db, vector_hits: list[dict]) -> list[FlaggedItem]:
        return assemble_flagged(db, vector_hits)  # full sorted list; caller caps and reports the total
