#!/usr/bin/env python3
"""FraudCase AI — Standalone mock server (stdlib only, no dependencies).

Serves:
  GET  /           -> index.html
  GET  /styles.css -> styles.css
  GET  /js/*       -> frontend modules
  GET  /styles/*   -> CSS modules
  POST /api/audit-case              {text} -> {case_id}
  GET  /api/events/{case_id}      text/event-stream of AgentEvent (SSE)
  POST /api/approve/{case_id}     ApprovalDecision -> {ok: true}
  GET  /api/report/{case_id}      -> AuditReport

Usage:
    python fraudcase_ai/web/mockserver.py
    open http://localhost:8000
"""

import http.server
import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── configuration ─────────────────────────────────────────────────────────────
PORT = 8000
WEB_DIR = Path(__file__).parent

# ── in-memory audit case store ─────────────────────────────────────────────────
cases: dict[str, dict] = {}   # case_id -> {queue, approval_event, approval_data, report}

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def maestro_context(event_type: str, gate: str | None = None) -> tuple[str, str]:
    if event_type == "plan":
        return "audit_plan_review", "external_ai_agent"
    if event_type == "awaiting_approval":
        stage = "audit_plan_review" if gate == "plan" else "exception_review"
        return stage, "human"
    if event_type in {"tool_call", "tool_result"}:
        return "agent_investigation", "external_ai_agent"
    if event_type in {"exception", "proposal"}:
        return "exception_review", "external_ai_agent"
    if event_type == "written":
        return "audit_log_write", "service_task"
    if event_type == "report_ready":
        return "audit_report_ready", "service_task"
    if event_type == "done":
        return "closed", "system"
    return "case_intake", "system"

def make_event(case_id: str, event_type: str, data: dict) -> dict:
    maestro_stage, actor_type = maestro_context(event_type, data.get("gate"))
    if case_id in cases:
        cases[case_id]["maestro_stage"] = maestro_stage
    return {
        "case_id": case_id,
        "type": event_type,
        "data": data,
        "maestro_stage": maestro_stage,
        "actor_type": actor_type,
        "ts": utcnow_iso(),
    }

# ── scripted event sequence ────────────────────────────────────────────────────
FLAGGED_ITEMS = [
    {
        "invoice_id": "INV-2024-0471",
        "vendor_name": "Apex Office Supplies Ltd",
        "department": "Operations",
        "amount": 48750.00,
        "reasons": ["duplicate", "off_hours"],
        "similarity": None,
        "detail": "Exact duplicate of INV-2024-0389; submitted at 02:14 AM",
    },
    {
        "invoice_id": "INV-2024-0512",
        "vendor_name": "Meridian Consulting Group",
        "department": "Finance",
        "amount": 125000.00,
        "reasons": ["policy_violation", "ghost_vendor"],
        "similarity": None,
        "detail": "Amount exceeds $100k policy cap; vendor onboarded <7 days ago with no EIN match",
    },
    {
        "invoice_id": "INV-2024-0388",
        "vendor_name": "NovaTech Dynamics LLC",
        "department": "IT",
        "amount": 31200.00,
        "reasons": ["near_duplicate", "vector_similar"],
        "similarity": 0.93,
        "detail": "98% structural match to INV-2024-0301; semantic similarity 0.93 vs fraud exemplar",
    },
    {
        "invoice_id": "INV-2024-0604",
        "vendor_name": "Zephyr Global Trade Co",
        "department": "Procurement",
        "amount": 67500.00,
        "reasons": ["ofac_hit", "ghost_vendor"],
        "similarity": None,
        "detail": "Vendor name fuzzy-matched OFAC SDN list (score 0.88); registered 3 days ago",
    },
    {
        "invoice_id": "INV-2024-0490",
        "vendor_name": "Pinnacle Freight Solutions",
        "department": "Logistics",
        "amount": 19800.00,
        "reasons": ["off_hours", "near_duplicate"],
        "similarity": 0.81,
        "detail": "Submitted Sunday 23:45; near-duplicate of two prior freight invoices this month",
    },
]

TOTAL_AT_RISK = sum(i["amount"] for i in FLAGGED_ITEMS)

AUDIT_MARKDOWN = f"""# Audit Report — Vendor Payment Review
**Generated:** {utcnow_iso()[:10]}
**Case Objective:** Audit this month's vendor payments

---

## Executive Summary

The AI audit agent reviewed **142 invoices** across **38 vendors** for the current billing period. **5 suspicious items** were identified totalling **${TOTAL_AT_RISK:,.0f}** at risk.

---

## Flagged Items

| Invoice | Vendor | Dept | Amount | Flags |
|---|---|---|---|---|
| INV-2024-0471 | Apex Office Supplies Ltd | Operations | $48,750 | duplicate, off_hours |
| INV-2024-0512 | Meridian Consulting Group | Finance | $125,000 | policy_violation, ghost_vendor |
| INV-2024-0388 | NovaTech Dynamics LLC | IT | $31,200 | near_duplicate, vector_similar |
| INV-2024-0604 | Zephyr Global Trade Co | Procurement | $67,500 | ofac_hit, ghost_vendor |
| INV-2024-0490 | Pinnacle Freight Solutions | Logistics | $19,800 | off_hours, near_duplicate |

---

## Risk Breakdown

- **Duplicate / Near-duplicate:** 3 items — $99,750
- **Ghost vendors:** 2 items — $192,500
- **OFAC hit:** 1 item — $67,500
- **Policy violations:** 1 item — $125,000
- **Off-hours activity:** 2 items — $68,550

---

## Recommendations

1. Immediately freeze payment on INV-2024-0512 and INV-2024-0604 pending legal review.
2. Escalate Zephyr Global Trade Co. to compliance for OFAC screening.
3. Audit the approver chain for INV-2024-0471 (duplicate flagged twice this quarter).
4. Review vendor onboarding controls — two ghost vendors passed screening.
5. Implement after-hours submission alerting for invoices >$10k.

---

*Report generated by FraudCase AI. For demo purposes only.*
"""

def process_audit_case_sequence(case_id: str):
    """Replay the scripted event sequence, pausing at approval gates."""
    q: queue.Queue = cases[case_id]["queue"]
    plan_gate: threading.Event = cases[case_id]["plan_gate"]
    action_gate: threading.Event = cases[case_id]["action_gate"]

    def send(event_type: str, data: dict, delay: float = 0.8):
        time.sleep(delay)
        q.put(make_event(case_id, event_type, data))

    # 1. Plan
    plan_text = (
        "Step 1: Retrieve all invoices for the current billing period from UiPath Data Service.\n"
        "Step 2: Run deduplication check — exact hash match + structural near-duplicate (Jaccard ≥ 0.85).\n"
        "Step 3: Retrieve semantically similar fraud evidence from UiPath Context Grounding (score ≥ 0.80).\n"
        "Step 4: Check each vendor against OFAC SDN list (fuzzy match ≥ 0.80).\n"
        "Step 5: Apply policy rules — flag invoices exceeding category caps.\n"
        "Step 6: Flag off-hours submissions (midnight–5 AM, weekends).\n"
        "Step 7: Assemble proposal of flagged items for human review.\n"
        "Step 8: Commit approved flags to the audit log and generate report."
    )
    send("plan", {"plan": plan_text, "text": plan_text}, delay=0.5)

    # Gate 1: awaiting plan approval
    send("awaiting_approval", {"gate": "plan", "plan": plan_text}, delay=0.6)
    plan_gate.wait()  # Block until UI posts /api/approve gate=plan

    approval = cases[case_id].get("plan_decision", {})
    approved = approval.get("approved", True)
    effective_plan = approval.get("edited_plan") or plan_text
    if not approved:
        send("done", {"message": "Plan rejected by user."}, delay=0.3)
        return

    # 2. Tool calls
    send("tool_call", {
        "tool_name": "fetch_invoices",
        "name": "fetch_invoices",
        "args": {"period": "2024-current", "limit": 200}
    }, delay=0.7)
    send("tool_result", {
        "tool_name": "fetch_invoices",
        "name": "fetch_invoices",
        "count": 142,
        "vendor_count": 38,
        "total_value": 2847632.50,
    }, delay=1.0)

    send("tool_call", {
        "tool_name": "dedup_check",
        "name": "dedup_check",
        "args": {"method": "hash+jaccard", "threshold": 0.85}
    }, delay=0.8)
    send("tool_result", {
        "tool_name": "dedup_check",
        "name": "dedup_check",
        "count": 3,
        "hit_count": 3,
    }, delay=1.1)

    send("tool_call", {
        "tool_name": "context_grounding_query",
        "name": "context_grounding_query",
        "args": {"index": "fraudcase-ai-evidence", "threshold": 0.80, "exemplar_count": 12}
    }, delay=0.7)
    send("tool_result", {
        "tool_name": "context_grounding_query",
        "name": "context_grounding_query",
        "count": 2,
        "hit_count": 2,
        "similarity_scores": [0.93, 0.81, 0.78, 0.74, 0.71, 0.68, 0.63, 0.59],
    }, delay=1.3)

    send("tool_call", {
        "tool_name": "ofac_screen",
        "name": "ofac_screen",
        "args": {"fuzzy_threshold": 0.80, "vendor_count": 38}
    }, delay=0.8)
    send("tool_result", {
        "tool_name": "ofac_screen",
        "name": "ofac_screen",
        "count": 1,
        "hit_count": 1,
        "top_match": {"vendor": "Zephyr Global Trade Co", "score": 0.88},
    }, delay=1.0)

    send("tool_call", {
        "tool_name": "policy_check",
        "name": "policy_check",
        "args": {"rules_loaded": 14}
    }, delay=0.7)
    send("tool_result", {
        "tool_name": "policy_check",
        "name": "policy_check",
        "count": 1,
        "hit_count": 1,
    }, delay=0.9)

    send("tool_call", {
        "tool_name": "off_hours_check",
        "name": "off_hours_check",
        "args": {"window": "00:00-05:00 + weekends"}
    }, delay=0.6)
    send("tool_result", {
        "tool_name": "off_hours_check",
        "name": "off_hours_check",
        "count": 2,
        "hit_count": 2,
    }, delay=0.8)

    # 3. Proposal
    send("proposal", {
        "items": FLAGGED_ITEMS,
        "total_at_risk": TOTAL_AT_RISK,
        "item_count": len(FLAGGED_ITEMS),
    }, delay=0.9)

    # Gate 2: awaiting action approval
    send("awaiting_approval", {
        "gate": "action",
        "item_count": len(FLAGGED_ITEMS),
    }, delay=0.5)
    action_gate.wait()  # Block until UI posts /api/approve gate=action

    decision = cases[case_id].get("action_decision", {})
    approved_ids = decision.get("approved_ids", [item["invoice_id"] for item in FLAGGED_ITEMS])
    rejected_ids = decision.get("rejected_ids", [])

    # 4. Written
    send("written", {
        "written_count": len(approved_ids),
        "approved_ids": approved_ids,
        "rejected_ids": rejected_ids,
    }, delay=1.2)

    # 5. Report ready
    report = {
        "case_id": case_id,
        "case_objective": cases[case_id]["case_objective"],
        "generated_at": utcnow_iso(),
        "flagged_count": len(approved_ids),
        "total_at_risk": sum(
            item["amount"] for item in FLAGGED_ITEMS
            if item["invoice_id"] in approved_ids
        ),
        "items": [item for item in FLAGGED_ITEMS if item["invoice_id"] in approved_ids],
        "markdown": AUDIT_MARKDOWN,
    }
    cases[case_id]["report"] = report

    send("report_ready", {
        "case_id": case_id,
        "flagged_count": report["flagged_count"],
        "total_at_risk": report["total_at_risk"],
    }, delay=1.0)

    # 6. Done
    send("done", {"case_id": case_id}, delay=0.5)
    q.put(None)  # sentinel


# ── HTTP handler ───────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[mock] {self.address_string()} {fmt % args}")

    # ── routing ──────────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            self._serve_file(WEB_DIR / "index.html", "text/html")
        elif path == "/styles.css":
            self._serve_file(WEB_DIR / "styles.css", "text/css")
        elif path == "/app.js":
            self._serve_file(WEB_DIR / "app.js", "application/javascript")
        elif path.startswith("/js/") and path.endswith(".js"):
            self._serve_static_asset(path, "application/javascript")
        elif path.startswith("/styles/") and path.endswith(".css"):
            self._serve_static_asset(path, "text/css")
        elif path.startswith("/api/events/"):
            case_id = path.removeprefix("/api/events/")
            self._sse_stream(case_id)
        elif path.startswith("/api/report/"):
            case_id = path.removeprefix("/api/report/")
            self._get_report(case_id)
        elif path.startswith("/api/maestro/audit-cases/"):
            case_id = path.removeprefix("/api/maestro/audit-cases/")
            self._get_maestro_case(case_id)
        elif path == "/api/status":
            self._json({
                "agent_runtime": "mock",
                "system_of_record": "UiPath Data Service",
                "evidence_engine": "UiPath Context Grounding",
                "context_grounding_index": "fraudcase-ai-evidence",
                "reasoning_engine": "Deterministic coded agent",
                "track": "UiPath AgentHack Track 1",
                "orchestration_layer": "UiPath Maestro Case",
                "case_management": True,
                "handoffs": ["human", "external_ai_agent", "service_task"],
                "human_approval": True,
                "audit_trail": True,
            })
        elif path == "/api/stats":
            self._json({
                "invoices": 1500,
                "vendors": 60,
                "total_spend": 42000000,
                "source": "mockserver",
            })
        elif path == "/healthz":
            self._json({"status": "ok"})
        else:
            self._not_found()

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._read_body()

        if path in {"/api/audit-case", "/api/maestro/audit-cases"}:
            self._post_audit_case(body)
        elif path.startswith("/api/maestro/audit-cases/") and path.endswith("/decisions"):
            case_id = path.removeprefix("/api/maestro/audit-cases/").removesuffix("/decisions")
            self._post_approve(case_id, body)
        elif path.startswith("/api/approve/"):
            case_id = path.removeprefix("/api/approve/")
            self._post_approve(case_id, body)
        else:
            self._not_found()

    # OPTIONS for CORS (dev convenience)
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    # ── handlers ─────────────────────────────────────────────────────────────
    def _post_audit_case(self, body: dict):
        text = body.get("text", "").strip()
        if not text:
            self._error(400, "text is required")
            return

        case_id = str(uuid.uuid4())
        cases[case_id] = {
            "case_objective": text,
            "queue": queue.Queue(),
            "plan_gate": threading.Event(),
            "action_gate": threading.Event(),
            "plan_decision": {},
            "action_decision": {},
            "maestro_stage": "case_intake",
            "report": None,
        }
        t = threading.Thread(target=process_audit_case_sequence, args=(case_id,), daemon=True)
        t.start()

        self._json({"case_id": case_id})

    def _sse_stream(self, case_id: str):
        if case_id not in cases:
            self._error(404, "audit case not found")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self._cors_headers()
        self.end_headers()

        q: queue.Queue = cases[case_id]["queue"]
        try:
            while True:
                evt = q.get(timeout=120)
                if evt is None:  # sentinel — done
                    break
                event_type = evt["type"]
                data_str = json.dumps(evt)
                msg = f"event: {event_type}\ndata: {data_str}\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except queue.Empty:
            pass

    def _post_approve(self, case_id: str, body: dict):
        if case_id not in cases:
            self._error(404, "audit case not found")
            return

        gate = body.get("gate", "")
        if gate == "plan":
            cases[case_id]["plan_decision"] = body
            cases[case_id]["plan_gate"].set()
        elif gate == "action":
            cases[case_id]["action_decision"] = body
            cases[case_id]["action_gate"].set()
        else:
            self._error(400, f"unknown gate: {gate}")
            return

        self._json({"ok": True})

    def _get_report(self, case_id: str):
        if case_id not in cases:
            self._error(404, "audit case not found")
            return
        report = cases[case_id].get("report")
        if not report:
            self._error(404, "report not ready yet")
            return
        self._json(report)

    def _get_maestro_case(self, case_id: str):
        if case_id not in cases:
            self._error(404, "audit case not found")
            return
        report = cases[case_id].get("report")
        self._json({
            "case_id": case_id,
            "case_objective": cases[case_id]["case_objective"],
            "maestro_stage": cases[case_id].get("maestro_stage", "case_intake"),
            "pending_gate": None,
            "exception_count": len(FLAGGED_ITEMS),
            "report_ready": report is not None,
        })

    # ── helpers ───────────────────────────────────────────────────────────────
    def _serve_static_asset(self, request_path: str, content_type: str):
        relative = Path(request_path.lstrip("/"))
        if ".." in relative.parts:
            self._not_found()
            return
        self._serve_file(WEB_DIR / relative, content_type)

    def _serve_file(self, path: Path, content_type: str):
        if not path.exists():
            self._error(404, f"file not found: {path.name}")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj: dict, status: int = 200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: int, msg: str):
        self._json({"error": msg}, status=code)

    def _not_found(self):
        self._error(404, "not found")

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return {}


# ── entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("", PORT), Handler)
    print(f"FraudCase AI mock server running at http://localhost:{PORT}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
