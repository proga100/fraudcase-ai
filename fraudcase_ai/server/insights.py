"""Read-only insight endpoints' logic: integration status, dataset stats, assistant.

Everything here is best-effort and never raises to the route layer — the UI must
always get a sane payload (the dashboard may not be empty, the status strip may
not break the page). No secrets ever leave this module: connection strings, key
paths and project IDs stay server-side.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any, Optional

from fraudcase_ai.config import DATA_DIR, get_settings
from fraudcase_ai.models import AskResponse, AuditReport

log = logging.getLogger(__name__)

# Last-resort baseline matching the shape of the generated demo ledger, used only
# when the local demo_dataset files are not reachable.
_FALLBACK_STATS = {"invoices": 1000, "vendors": 80, "total_spend": 4_800_000.0, "source": "baseline"}


def get_status() -> dict:
    """Integration status for the UI strip. Safe to expose: no URIs, no keys."""
    s = get_settings()
    live = not s.use_mocks
    agent_runtime = "external_coded_agent" if live else "mock"
    return {
        "mode": "live" if live else "demo",
        # UiPath-first architecture: Data Service is the system of record and
        # Context Grounding owns embeddings, vector indexing, and retrieval.
        "system_of_record": "UiPath Data Service",
        "evidence_engine": "UiPath Context Grounding",
        "context_grounding_index": s.uipath_context_grounding_index_name,
        "reasoning_engine": "Deterministic coded agent",
        "agent_runtime": agent_runtime,
        "agent_runtime_label": {
            "mock": "Scripted demo runner",
            "external_coded_agent": "UiPath external coded agent",
        }[agent_runtime],
        "track": "UiPath AgentHack Track 1",
        "orchestration_layer": "UiPath Maestro Case",
        "case_management": True,
        "maestro_stages": [
            "case_intake",
            "audit_plan_review",
            "agent_investigation",
            "exception_review",
            "audit_log_write",
            "audit_report_ready",
            "closed",
        ],
        "handoffs": ["human", "external_ai_agent", "service_task"],
        "human_approval": True,   # structural gates — always on
        "audit_trail": True,      # gated writes land in the UiPath Data Service audit log
    }


@lru_cache(maxsize=1)
def get_stats() -> dict:
    """Dataset scope for the dashboard's first paint: invoices, vendors, total spend.

    Reads the local demo_dataset files (the same fixtures the UiPath clients fall
    back to without credentials), with a static baseline so the endpoint never
    fails. Cached per process — the in-scope dataset doesn't change during a demo.
    Per-case live data flows through the UiPath runner, not this first-paint stat.
    """
    try:
        invoices = json.loads((DATA_DIR / "invoices.json").read_text())
        vendors = json.loads((DATA_DIR / "vendors.json").read_text())
        return {
            "invoices": len(invoices),
            "vendors": len(vendors),
            "total_spend": float(sum(i.get("amount", 0.0) for i in invoices)),
            "source": "demo-dataset",
        }
    except Exception:  # noqa: BLE001
        return dict(_FALLBACK_STATS)


# --------------------------------------------------------------------------- #
# AI Audit Assistant ("Ask Audit Agent" drawer)
# --------------------------------------------------------------------------- #

def _template_invoice_answer(invoice_context: dict[str, Any]) -> str:
    reasons = invoice_context.get("reasons") or []
    reasons_text = ", ".join(str(r).replace("_", " ") for r in reasons) if not isinstance(reasons, str) else reasons
    amount = invoice_context.get("amount") or 0
    try:
        amount_text = f"${float(amount):,.0f}"
    except (TypeError, ValueError):
        amount_text = str(amount)
    detail = invoice_context.get("detail") or "No detailed evidence was supplied."
    invoice_id = invoice_context.get("invoice_id", "this invoice")
    vendor = invoice_context.get("vendor_name", "the vendor")
    dept = invoice_context.get("department", "the department")
    return (
        f"Invoice {invoice_id} from {vendor} ({dept}) is flagged for "
        f"{reasons_text or 'audit risk'} on a {amount_text} payment. Evidence: {detail}. "
        "The auditor should verify the vendor master record, PO or contract approval, "
        "duplicate invoice history, payment authorization, and whether the policy threshold "
        "or exception approval is documented before approving the item."
    )


def _template_answer(
    question: str,
    report: Optional[AuditReport],
    invoice_context: Optional[dict[str, Any]] = None,
) -> str:
    """Deterministic grounded answer used in demo mode / as the LLM fallback."""
    if invoice_context:
        return _template_invoice_answer(invoice_context)
    stats = get_stats()
    if report is None:
        return (
            f"No completed audit case is available yet. Dataset in scope: "
            f"{stats['invoices']} invoices, {stats['vendors']} vendors, "
            f"${stats['total_spend']:,.0f} total spend. Open an audit case first, "
            "then I can cite flagged invoices, evidence, and next actions."
        )
    top = sorted(report.items, key=lambda i: i.amount, reverse=True)[:3]
    top_lines = "".join(
        f"\n• {it.invoice_id} — {it.vendor_name} — ${it.amount:,.0f} "
        f"({', '.join(r.value.replace('_', ' ') for r in it.reasons)})"
        for it in top
    )
    return (
        f"Regarding “{question.strip()}”: the audit case “{report.case_objective}” flagged "
        f"{report.flagged_count} invoices worth ${report.total_at_risk:,.0f} at risk. "
        f"Highest-value flagged items:{top_lines}\n"
        f"Each flag carries its evidence in the Findings tab; every write was approved "
        f"by a human auditor before it reached the audit log."
    )


def answer_question(
    question: str,
    report: Optional[AuditReport],
    invoice_context: Optional[dict[str, Any]] = None,
) -> AskResponse:
    """Answer with a deterministic, grounded template.

    The assistant stays fully UiPath-first: it cites only the flagged findings and
    Context-Grounding evidence already present in the case, with no external LLM call.
    """
    answer = _template_answer(question, report, invoice_context)
    model = "demo-template" if get_settings().use_mocks else "uipath-grounded"
    return AskResponse(answer=answer, model=model)
