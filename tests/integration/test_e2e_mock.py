"""Phase 2 E2E: a full audit case through the REAL UiPath-first slices, no creds.

records (UiPath Data Service shape) -> detector suite (assemble_flagged_records)
        -> two-gate approval (GateMachine) -> audit-log write (DataServiceStore)
        -> report (render_report)

Proves the independently-built slices compose into a correct end-to-end audit on
the local fallback path, with no MongoDB and no embedding model in sight.
"""

from __future__ import annotations

import pytest

from fraudcase_ai.agent.gates import GateMachine
from fraudcase_ai.agent.report import render_report
from fraudcase_ai.models import ApprovalDecision, ApprovalGate, FlaggedReason
from fraudcase_ai.tools.triage import assemble_flagged_records
from fraudcase_ai.uipath.clients import DataServiceStore

CASE_ID = "case-e2e-1"
CASE_OBJECTIVE = "Audit this month's vendor payments"


def _reasons_for(items, invoice_id):
    return next((set(it.reasons) for it in items if it.invoice_id == invoice_id), set())


def _investigate(records):
    transactions, vendors, policies = records
    # Context Grounding surfaces i3 (near-duplicate) as semantic evidence.
    hits = [{"invoice_id": "i3", "score": 0.95}]
    return assemble_flagged_records(transactions, vendors, policies, hits)


@pytest.mark.anyio
async def test_full_audit_case_finds_planted_fraud_and_writes_report(records):
    # 1. Detector suite over UiPath Data Service records + Context Grounding hits
    items = _investigate(records)
    flagged_ids = {it.invoice_id for it in items}

    # the planted fraud must be caught by the right detectors
    assert "i2" in flagged_ids and FlaggedReason.DUPLICATE in _reasons_for(items, "i2")
    assert "i4" in flagged_ids and FlaggedReason.POLICY_VIOLATION in _reasons_for(items, "i4")
    assert "i5" in flagged_ids and FlaggedReason.OFF_HOURS in _reasons_for(items, "i5")
    assert "i6" in flagged_ids and FlaggedReason.GHOST_VENDOR in _reasons_for(items, "i6")
    assert "i3" in flagged_ids and FlaggedReason.VECTOR_SIMILAR in _reasons_for(items, "i3")

    # 2. Two-gate human-in-the-loop
    gm = GateMachine(CASE_ID)
    gm.submit_plan("1. read data service 2. context grounding 3. detectors 4. propose")
    assert gm.status == "awaiting_plan"
    gm.approve_plan(ApprovalDecision(gate=ApprovalGate.PLAN, approved=True))
    assert gm.status == "executing"

    gm.propose(items)
    assert gm.status == "awaiting_action"
    approved_ids = gm.approve_action(
        ApprovalDecision(gate=ApprovalGate.ACTION, approved_ids=list(flagged_ids))
    )
    assert set(approved_ids) == flagged_ids

    # 3. Gated write back to UiPath Data Service (local fallback returns the payload)
    approved_items = [it for it in items if it.invoice_id in set(approved_ids)]
    store = DataServiceStore()
    payload = await store.write_audit_log(CASE_ID, CASE_OBJECTIVE, approved_items)
    assert payload["case_id"] == CASE_ID
    assert payload["count"] == len(flagged_ids)
    assert set(payload["invoice_ids"]) == flagged_ids
    assert payload["source"] == "uipath_data_service"

    # 4. Report
    report = render_report(CASE_ID, CASE_OBJECTIVE, items)
    assert report.flagged_count == len(items)
    assert report.total_at_risk == sum(it.amount for it in items)
    assert "#" in report.markdown  # has a markdown heading


def test_rejecting_plan_blocks_execution():
    gm = GateMachine("case-reject")
    gm.submit_plan("plan")
    gm.approve_plan(ApprovalDecision(gate=ApprovalGate.PLAN, approved=False))
    assert gm.status in ("done", "error")  # rejected plan must not reach executing
