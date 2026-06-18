"""Tests for UiPathRunner — the live external coded agent, with mocked UiPath clients.

Drives the runner through the real server harness (_process_case + store) exactly
like production, but injects fake Data Service + Context Grounding clients so the
flow runs with no network and no credentials.
"""

from __future__ import annotations

import asyncio

import pytest

from fraudcase_ai.models import (
    AgentEvent,
    ApprovalDecision,
    ApprovalGate,
    AuditCaseRequest,
    EventType,
)
from fraudcase_ai.server.app import _process_case
from fraudcase_ai.server.store import AuditCaseStore
from fraudcase_ai.server.uipath_runner import UiPathRunner


# --------------------------------------------------------------------------- #
# Fake UiPath clients
# --------------------------------------------------------------------------- #

class FakeStore:
    """Stand-in for DataServiceStore that serves fixture records and records writes."""

    def __init__(self, records):
        self._records = records
        self.written: list[dict] = []

    @property
    def configured(self) -> bool:
        return True

    async def load_case_dataset(self):
        return self._records

    async def write_audit_log(self, case_id, objective, approved_items):
        payload = {
            "case_id": case_id,
            "objective": objective,
            "invoice_ids": [i.invoice_id for i in approved_items],
            "count": len(approved_items),
        }
        self.written.append(payload)
        return payload


class FakeRetriever:
    """Stand-in for ContextGroundingRetriever; returns hits or raises on demand."""

    index_name = "fraudcase-ai-evidence"

    def __init__(self, hits=None, raise_exc: Exception | None = None):
        self._hits = hits or []
        self._raise = raise_exc

    @property
    def configured(self) -> bool:
        return True

    async def query(self, objective, *, limit=8):
        if self._raise is not None:
            raise self._raise
        return self._hits


# --------------------------------------------------------------------------- #
# Driver — mirrors the production _process_case + approval flow
# --------------------------------------------------------------------------- #

async def _run_case(
    runner: UiPathRunner,
    objective: str,
    plan_decision: ApprovalDecision,
    action_decision: ApprovalDecision | None = None,
) -> list[AgentEvent]:
    store = AuditCaseStore()
    record = store.create_case(AuditCaseRequest(text=objective), runner)
    case_id = record.case_id
    task = asyncio.create_task(_process_case(case_id, store))

    events: list[AgentEvent] = []
    posted_plan = posted_action = False
    while True:
        ev = await asyncio.wait_for(record.queue.get(), timeout=5.0)
        if ev is None:
            break
        events.append(ev)
        if ev.type == EventType.AWAITING_APPROVAL:
            gate = ev.data.get("gate")
            if gate == ApprovalGate.PLAN.value and not posted_plan:
                await store.push_approval(case_id, plan_decision)
                posted_plan = True
            elif gate == ApprovalGate.ACTION.value and not posted_action:
                await store.push_approval(
                    case_id,
                    action_decision or ApprovalDecision(gate=ApprovalGate.ACTION, approved=True),
                )
                posted_action = True
        if ev.type == EventType.DONE:
            break
    await task
    return events


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_full_flow_with_mocked_uipath_clients(records):
    store = FakeStore(records)
    retriever = FakeRetriever(hits=[{"invoice_id": "i3", "score": 0.95}])
    runner = UiPathRunner(store=store, retriever=retriever)

    events = await _run_case(
        runner,
        "Audit this month's vendor payments",
        ApprovalDecision(gate=ApprovalGate.PLAN, approved=True),
        ApprovalDecision(gate=ApprovalGate.ACTION, approved=True),
    )

    types = [e.type for e in events]
    for expected in (
        EventType.PLAN, EventType.AWAITING_APPROVAL, EventType.TOOL_CALL,
        EventType.TOOL_RESULT, EventType.PROPOSAL, EventType.WRITTEN,
        EventType.REPORT_READY, EventType.DONE,
    ):
        assert expected in types, f"missing {expected}; got {types}"
    assert types[-1] == EventType.DONE

    # The planted fraud surfaced in the proposal.
    proposal = next(e for e in events if e.type == EventType.PROPOSAL)
    flagged_ids = {it["invoice_id"] for it in proposal.data["items"]}
    assert {"i2", "i4", "i5", "i6", "i3"}.issubset(flagged_ids)

    # The gated write hit the (mocked) Data Service exactly once.
    assert len(store.written) == 1
    assert store.written[0]["count"] == len(flagged_ids)

    # Tool labels prove the UiPath-first integration story.
    tool_results = [e for e in events if e.type == EventType.TOOL_RESULT]
    labels = {e.data.get("tool_label") for e in tool_results}
    assert "UiPath Context Grounding" in labels
    assert "UiPath Data Service · read" in labels


@pytest.mark.anyio
async def test_gate1_rejection_stops_before_any_write(records):
    store = FakeStore(records)
    runner = UiPathRunner(store=store, retriever=FakeRetriever())

    events = await _run_case(
        runner,
        "Audit payments",
        ApprovalDecision(gate=ApprovalGate.PLAN, approved=False),
    )

    types = [e.type for e in events]
    assert EventType.ERROR in types
    assert EventType.WRITTEN not in types
    assert types[-1] == EventType.DONE
    assert store.written == []


@pytest.mark.anyio
async def test_gate2_rejection_writes_only_kept_subset(records):
    store = FakeStore(records)
    retriever = FakeRetriever(hits=[{"invoice_id": "i3", "score": 0.95}])
    runner = UiPathRunner(store=store, retriever=retriever)

    events = await _run_case(
        runner,
        "Audit payments",
        ApprovalDecision(gate=ApprovalGate.PLAN, approved=True),
        ApprovalDecision(gate=ApprovalGate.ACTION, approved=True, rejected_ids=["i6"]),
    )

    written = next(e for e in events if e.type == EventType.WRITTEN)
    proposal = next(e for e in events if e.type == EventType.PROPOSAL)
    proposed = {it["invoice_id"] for it in proposal.data["items"]}
    assert written.data["flagged"] == len(proposed) - 1  # i6 rejected
    assert store.written[0]["count"] == len(proposed) - 1
    assert "i6" not in store.written[0]["invoice_ids"]


@pytest.mark.anyio
async def test_context_grounding_failure_routes_recoverable_exception(records):
    store = FakeStore(records)
    retriever = FakeRetriever(raise_exc=RuntimeError("context grounding down"))
    runner = UiPathRunner(store=store, retriever=retriever)

    events = await _run_case(
        runner,
        "Audit payments",
        ApprovalDecision(gate=ApprovalGate.PLAN, approved=True),
        ApprovalDecision(gate=ApprovalGate.ACTION, approved=True),
    )

    exceptions = [e for e in events if e.type == EventType.EXCEPTION]
    assert len(exceptions) == 1
    assert exceptions[0].data["exception_type"] == "context_grounding_recoverable"
    assert exceptions[0].maestro_stage.value == "exception_review"

    # The case still completes: deterministic detectors carry it through.
    types = [e.type for e in events]
    assert EventType.WRITTEN in types
    assert types[-1] == EventType.DONE
    # Deterministic detectors still caught the planted non-retrieval fraud.
    proposal = next(e for e in events if e.type == EventType.PROPOSAL)
    flagged_ids = {it["invoice_id"] for it in proposal.data["items"]}
    assert {"i2", "i4", "i5", "i6"}.issubset(flagged_ids)
