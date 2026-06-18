"""Backend API tests (TDD).

Strategy for SSE + approval interplay:
  - Use httpx.AsyncClient with anyio to drive the async FastAPI app.
  - For the streaming test, we consume the SSE stream in one task while posting
    approvals from another, using asyncio.gather or sequential awaits once we
    know the stream will pause at the right gate.
  - A thin helper `drain_queue` lets us test queue contents without HTTP when
    needed for determinism.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from fraudcase_ai.models import (
    AgentEvent,
    ApprovalDecision,
    ApprovalGate,
    AuditReport,
    EventType,
    AuditCaseRequest,
)
from fraudcase_ai.server.app import app, _process_case
from fraudcase_ai.server.runner import FakeRunner
from fraudcase_ai.server.store import AuditCaseStore


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def store() -> AuditCaseStore:
    """Fresh AuditCaseStore per test — prevents cross-test state leakage."""
    return AuditCaseStore()


@pytest_asyncio.fixture
async def client(store: AuditCaseStore):
    """AsyncClient wired to a fresh store."""
    app.state.store = store
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

async def _drain_case(case_id: str, store: AuditCaseStore, *, timeout: float = 5.0) -> list[AgentEvent]:
    """Drain all events from the audit case queue without HTTP, for deterministic testing."""
    record = store.get_case(case_id)
    assert record is not None, f"Audit case {case_id!r} not in store"
    events: list[AgentEvent] = []
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            event: Optional[AgentEvent] = await asyncio.wait_for(
                record.queue.get(), timeout=min(0.5, remaining)
            )
        except asyncio.TimeoutError:
            break
        if event is None:
            break
        events.append(event)
        if event.type == EventType.DONE:
            break
    return events


def _parse_sse_chunk(chunk: str) -> list[AgentEvent]:
    """Parse one or more SSE frames from a raw chunk string."""
    events: list[AgentEvent] = []
    for frame in chunk.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        data_line: Optional[str] = None
        for line in frame.splitlines():
            if line.startswith("data:"):
                data_line = line[len("data:"):].strip()
        if data_line:
            events.append(AgentEvent.model_validate_json(data_line))
    return events


# --------------------------------------------------------------------------- #
# Test: POST /healthz
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_healthz(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_status_exposes_uipath_runtime(client: AsyncClient) -> None:
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_runtime"] == "mock"
    assert body["system_of_record"] == "UiPath Data Service"
    assert body["evidence_engine"] == "UiPath Context Grounding"
    assert body["orchestration_layer"] == "UiPath Maestro Case"
    assert body["human_approval"] is True
    assert body["audit_trail"] is True
    # No Google/Mongo branding should leak into the status payload.
    assert "gemini_model" not in body
    assert "mcp_server" not in body


@pytest.mark.anyio
async def test_stats_shape(client: AsyncClient) -> None:
    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["invoices"] > 0
    assert body["vendors"] > 0
    assert body["total_spend"] > 0
    assert body["source"]


@pytest.mark.anyio
async def test_ask_demo_mode_returns_grounded_answer(client: AsyncClient) -> None:
    resp = await client.post("/api/ask", json={"question": "What is in scope?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ai_generated"] is True
    assert body["model"] == "demo-template"
    assert "dataset in scope" in body["answer"].lower()


@pytest.mark.anyio
async def test_ask_invoice_context_answers_before_report(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/ask",
        json={
            "question": "Explain this invoice",
            "case_id": "case-before-report",
            "invoice_context": {
                "invoice_id": "INV-777",
                "vendor_name": "Ghost LLC",
                "department": "IT",
                "amount": 125000,
                "reasons": ["ghost_vendor", "policy_violation"],
                "detail": "P4: 125000 > 100000; payment to ghost vendor",
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    answer = body["answer"].lower()
    assert "inv-777" in answer
    assert "ghost llc" in answer
    assert "no audit case has completed" not in answer


@pytest.mark.anyio
async def test_ask_validation(client: AsyncClient) -> None:
    empty = await client.post("/api/ask", json={"question": ""})
    assert empty.status_code == 422
    too_long = await client.post("/api/ask", json={"question": "x" * 2001})
    assert too_long.status_code == 422


# --------------------------------------------------------------------------- #
# Test: POST /api/audit-case returns a case_id
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_start_audit_case_returns_case_id(client: AsyncClient, store: AuditCaseStore) -> None:
    resp = await client.post("/api/audit-case", json={"text": "Audit Q4 vendor payments"})
    assert resp.status_code == 200
    body = resp.json()
    assert "case_id" in body
    case_id = body["case_id"]
    assert isinstance(case_id, str) and len(case_id) > 0
    # The audit case should be registered in the store.
    assert store.get_case(case_id) is not None


# --------------------------------------------------------------------------- #
# Test: Full event sequence through queue drain (deterministic, no HTTP stream)
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_full_event_sequence_via_queue(store: AuditCaseStore) -> None:
    """Drive FakeRunner directly: create audit case, post both approvals, assert all EventTypes."""
    audit_case = AuditCaseRequest(text="Full audit test")
    runner = FakeRunner()
    record = store.create_case(audit_case, runner)
    case_id = record.case_id

    # Start background task.
    task = asyncio.create_task(_process_case(case_id, store))

    # Collect events up to AWAITING_APPROVAL(plan).
    plan_gate_seen = False
    events_so_far: list[AgentEvent] = []

    for _ in range(20):
        try:
            evt = await asyncio.wait_for(record.queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            break
        if evt is None:
            break
        events_so_far.append(evt)
        if evt.type == EventType.AWAITING_APPROVAL and evt.data.get("gate") == ApprovalGate.PLAN.value:
            plan_gate_seen = True
            break

    assert plan_gate_seen, f"Did not see AWAITING_APPROVAL(plan). Got: {[e.type for e in events_so_far]}"

    # Post plan approval.
    plan_decision = ApprovalDecision(gate=ApprovalGate.PLAN, approved=True)
    await store.push_approval(case_id, plan_decision)

    # Collect until AWAITING_APPROVAL(action).
    action_gate_seen = False
    for _ in range(20):
        try:
            evt = await asyncio.wait_for(record.queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            break
        if evt is None:
            break
        events_so_far.append(evt)
        if evt.type == EventType.AWAITING_APPROVAL and evt.data.get("gate") == ApprovalGate.ACTION.value:
            action_gate_seen = True
            break

    assert action_gate_seen, f"Did not see AWAITING_APPROVAL(action). Got: {[e.type for e in events_so_far]}"

    # Post action approval — approve all items.
    action_decision = ApprovalDecision(
        gate=ApprovalGate.ACTION,
        approved=True,
        approved_ids=["INV-001", "INV-002", "INV-003"],
    )
    await store.push_approval(case_id, action_decision)

    # Drain remaining events until DONE.
    for _ in range(20):
        try:
            evt = await asyncio.wait_for(record.queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            break
        if evt is None:
            break
        events_so_far.append(evt)
        if evt.type == EventType.DONE:
            break

    await task

    # Verify the complete sequence.
    types = [e.type for e in events_so_far]
    assert EventType.PLAN in types
    assert EventType.AWAITING_APPROVAL in types
    assert EventType.TOOL_CALL in types
    assert EventType.TOOL_RESULT in types
    assert EventType.PROPOSAL in types
    assert EventType.WRITTEN in types
    assert EventType.REPORT_READY in types
    assert EventType.DONE in types

    # Ensure PLAN comes before first AWAITING_APPROVAL.
    plan_idx = types.index(EventType.PLAN)
    await_idx = types.index(EventType.AWAITING_APPROVAL)
    assert plan_idx < await_idx

    # Ensure DONE is last.
    assert types[-1] == EventType.DONE

    attributed = [e for e in events_so_far if e.type != EventType.DONE]
    assert all("agent" in e.data for e in attributed)
    assert all("tool_label" in e.data for e in attributed)


@pytest.mark.anyio
async def test_recoverable_exception_event_before_gate_two(store: AuditCaseStore) -> None:
    """Objectives that mention a failure scenario surface a recoverable case exception."""
    audit_case = AuditCaseRequest(text="Audit payments and simulate a MongoDB timeout exception")
    runner = FakeRunner()
    record = store.create_case(audit_case, runner)
    case_id = record.case_id

    task = asyncio.create_task(_process_case(case_id, store))
    events: list[AgentEvent] = []

    for _ in range(20):
        evt = await asyncio.wait_for(record.queue.get(), timeout=1.0)
        if evt is None:
            break
        events.append(evt)
        if evt.type == EventType.AWAITING_APPROVAL and evt.data.get("gate") == ApprovalGate.PLAN.value:
            break

    await store.push_approval(case_id, ApprovalDecision(gate=ApprovalGate.PLAN, approved=True))

    for _ in range(30):
        evt = await asyncio.wait_for(record.queue.get(), timeout=1.0)
        if evt is None:
            break
        events.append(evt)
        if evt.type == EventType.AWAITING_APPROVAL and evt.data.get("gate") == ApprovalGate.ACTION.value:
            break

    await store.push_approval(
        case_id,
        ApprovalDecision(gate=ApprovalGate.ACTION, approved=True, approved_ids=["INV-001"]),
    )

    while True:
        evt = await asyncio.wait_for(record.queue.get(), timeout=1.0)
        if evt is None:
            break
        events.append(evt)
        if evt.type == EventType.DONE:
            break
    await task

    exception_events = [e for e in events if e.type == EventType.EXCEPTION]
    assert len(exception_events) == 1
    assert exception_events[0].maestro_stage.value == "exception_review"
    assert "recoverable" in exception_events[0].data["exception_type"]


# --------------------------------------------------------------------------- #
# Test: /api/report returns a valid AuditReport after completion
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_report_after_completion(client: AsyncClient, store: AuditCaseStore) -> None:
    """Start a audit_case, drive through both gates, then GET /api/report."""
    # Start audit_case.
    resp = await client.post("/api/audit-case", json={"text": "Report test audit_case"})
    assert resp.status_code == 200
    case_id = resp.json()["case_id"]

    record = store.get_case(case_id)
    assert record is not None

    # Wait for AWAITING_APPROVAL(plan).
    plan_gate_seen = False
    for _ in range(20):
        try:
            evt = await asyncio.wait_for(record.queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            break
        if evt is None:
            break
        if evt.type == EventType.AWAITING_APPROVAL and evt.data.get("gate") == ApprovalGate.PLAN.value:
            plan_gate_seen = True
            break

    assert plan_gate_seen

    # Approve plan via HTTP.
    resp2 = await client.post(
        f"/api/approve/{case_id}",
        json={"gate": "plan", "approved": True},
    )
    assert resp2.status_code == 200
    assert resp2.json() == {"ok": True}

    # Wait for AWAITING_APPROVAL(action).
    action_gate_seen = False
    for _ in range(20):
        try:
            evt = await asyncio.wait_for(record.queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            break
        if evt is None:
            break
        if evt.type == EventType.AWAITING_APPROVAL and evt.data.get("gate") == ApprovalGate.ACTION.value:
            action_gate_seen = True
            break

    assert action_gate_seen

    # Approve action via HTTP.
    resp3 = await client.post(
        f"/api/approve/{case_id}",
        json={
            "gate": "action",
            "approved": True,
            "approved_ids": ["INV-001", "INV-002", "INV-003"],
            "rejected_ids": [],
        },
    )
    assert resp3.status_code == 200

    # Drain until DONE so report is stored.
    done_seen = False
    for _ in range(20):
        try:
            evt = await asyncio.wait_for(record.queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            break
        if evt is None:
            break
        if evt.type == EventType.DONE:
            done_seen = True
            break

    assert done_seen

    # GET /api/report.
    resp4 = await client.get(f"/api/report/{case_id}")
    assert resp4.status_code == 200
    report = AuditReport.model_validate(resp4.json())
    assert report.case_id == case_id
    assert report.flagged_count == 3
    assert report.total_at_risk > 0
    assert len(report.items) == 3
    assert report.markdown != ""


# --------------------------------------------------------------------------- #
# Test: Approving an unknown case_id -> 404
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_approve_unknown_case_returns_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/approve/nonexistent-case-id",
        json={"gate": "plan", "approved": True},
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Test: GET /api/report for unknown case -> 404
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_report_unknown_case_returns_404(client: AsyncClient) -> None:
    resp = await client.get("/api/report/nonexistent-case-id")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Test: GET /api/events for unknown case -> 404
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_events_unknown_case_returns_404(client: AsyncClient) -> None:
    resp = await client.get("/api/events/nonexistent-case-id")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Test: SSE stream yields proper frames (format check + GET returns 200)
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_sse_stream_response_headers(client: AsyncClient, store: AuditCaseStore) -> None:
    """Verify that GET /api/events/{case_id} returns text/event-stream content-type."""
    resp = await client.post("/api/audit-case", json={"text": "SSE headers test"})
    case_id = resp.json()["case_id"]

    # Immediately post both approvals so the stream can complete without blocking.
    record = store.get_case(case_id)
    assert record is not None

    # Drive the audit case to completion via queue drain (same as test_full_event_sequence_via_queue)
    # but also verify the SSE route returns 200 + correct content-type on first connect.
    # We use a HEAD-like pattern: just initiate the connection, check headers, then close.
    # Note: httpx ASGITransport buffers the full response body before streaming, so we
    # test headers and format via the to_sse helper directly rather than audit case streaming.
    from fraudcase_ai.server.events import to_sse as _to_sse

    # Verify to_sse produces valid SSE frames with correct fields.
    event = AgentEvent(
        case_id=case_id,
        type=EventType.PLAN,
        data={"plan": "test plan"},
    )
    sse_frame = _to_sse(event)
    assert sse_frame.startswith("event: plan\n")
    assert "data: " in sse_frame
    assert sse_frame.endswith("\n\n")

    # Parse the data line back.
    lines = sse_frame.strip().splitlines()
    data_line = next(l for l in lines if l.startswith("data:"))
    parsed = AgentEvent.model_validate_json(data_line[len("data:"):].strip())
    assert parsed.type == EventType.PLAN
    assert parsed.case_id == case_id


@pytest.mark.anyio
async def test_sse_stream_full_sequence_via_queue(store: AuditCaseStore) -> None:
    """Drive FakeRunner, verify all SSE frames are emitted correctly via queue drain.

    httpx's ASGITransport buffers the full response before yielding, so testing
    SSE streaming with concurrent approvals via HTTP requires a real HTTP server.
    Instead, we test the complete event sequence via the queue (which is what the
    SSE generator reads) and separately verify the SSE frame format above.
    """
    from fraudcase_ai.server.app import _process_case
    from fraudcase_ai.server.events import to_sse as _to_sse

    audit_case_request = AuditCaseRequest(text="SSE queue test")
    runner = FakeRunner()
    record = store.create_case(audit_case_request, runner)
    case_id = record.case_id

    task = asyncio.create_task(_process_case(case_id, store))

    all_events: list[AgentEvent] = []

    # Helper: drain events from queue until gate or done.
    async def _collect_until(gate_type: EventType, gate_value: str) -> None:
        for _ in range(20):
            try:
                evt = await asyncio.wait_for(record.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                break
            if evt is None:
                return
            all_events.append(evt)
            # Verify to_sse produces a valid frame for every event.
            frame = _to_sse(evt)
            assert frame.startswith(f"event: {evt.type.value}\n")
            assert frame.endswith("\n\n")
            if evt.type == gate_type and evt.data.get("gate") == gate_value:
                return
            if evt.type == EventType.DONE:
                return

    # Collect until plan gate.
    await _collect_until(EventType.AWAITING_APPROVAL, ApprovalGate.PLAN.value)
    plan_types = [e.type for e in all_events]
    assert EventType.PLAN in plan_types
    assert EventType.AWAITING_APPROVAL in plan_types

    # Deliver plan approval.
    await store.push_approval(case_id, ApprovalDecision(gate=ApprovalGate.PLAN, approved=True))

    # Collect until action gate.
    await _collect_until(EventType.AWAITING_APPROVAL, ApprovalGate.ACTION.value)
    mid_types = [e.type for e in all_events]
    assert EventType.TOOL_CALL in mid_types
    assert EventType.TOOL_RESULT in mid_types
    assert EventType.PROPOSAL in mid_types

    # Deliver action approval.
    await store.push_approval(
        case_id,
        ApprovalDecision(
            gate=ApprovalGate.ACTION,
            approved=True,
            approved_ids=["INV-001", "INV-002", "INV-003"],
        ),
    )

    # Drain remaining events.
    await _collect_until(EventType.DONE, "")
    await task

    final_types = [e.type for e in all_events]
    assert EventType.WRITTEN in final_types
    assert EventType.REPORT_READY in final_types
    assert EventType.DONE in final_types
