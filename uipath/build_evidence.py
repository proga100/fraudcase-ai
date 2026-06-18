"""Build UiPath Context Grounding evidence documents from the demo dataset.

Each invoice becomes one evidence record whose text is rich enough for semantic
search and whose ``invoice_id`` is explicit (in the id, title, and content) so
Context Grounding search results map straight back to a finding.

Outputs (under uipath/context_grounding_evidence/):
  * evidence.jsonl  — one JSON object per line: {invoice_id, title, content, ...}
  * evidence.csv    — same data, flat columns, for the CSV ingestion path
  * docs/<invoice_id>.md  — only with --per-file (one document per invoice)

Usage:
  python uipath/build_evidence.py [--per-file]
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "demo_dataset"
OUT_DIR = Path(__file__).resolve().parent / "context_grounding_evidence"

FLAG_LABELS = {
    "is_duplicate": "exact duplicate invoice",
    "is_near_duplicate": "near-duplicate invoice",
    "is_policy_violation": "policy limit violation",
    "is_off_hours": "off-hours payment",
    "is_ghost_vendor": "payment to ghost vendor",
    "is_fraud_exemplar": "known fraud exemplar",
}


def _load() -> tuple[list[dict], dict[str, dict], list[dict]]:
    invoices = json.loads((DATA_DIR / "invoices.json").read_text())
    vendors = {v["vendor_id"]: v for v in json.loads((DATA_DIR / "vendors.json").read_text())}
    policies = json.loads((DATA_DIR / "policies.json").read_text())
    return invoices, vendors, policies


def _policy_cap(category: str, policies: list[dict]) -> dict | None:
    specific = next((p for p in policies if p.get("category") == category), None)
    if specific:
        return specific
    return next((p for p in policies if p.get("category") == "*"), None)


def _content(inv: dict, vendors: dict[str, dict], policies: list[dict]) -> str:
    v = vendors.get(inv.get("vendor_id"), {})
    cap = _policy_cap(inv.get("category", ""), policies)
    signals = [label for key, label in FLAG_LABELS.items() if inv.get(key)]
    parts = [
        f"Invoice {inv['invoice_id']} from vendor {inv.get('vendor_name')} "
        f"(vendor_id {inv.get('vendor_id')}).",
        f"Vendor country {v.get('country', 'unknown')}, risk score {v.get('risk_score', 'n/a')}"
        + (", flagged as a GHOST VENDOR." if v.get("is_ghost") else "."),
        f"Department {inv.get('department')}, category {inv.get('category')}.",
        f"Amount {inv.get('amount')} {inv.get('currency', 'USD')} via "
        f"{inv.get('payment_method')} on {inv.get('invoice_date')} at "
        f"{inv.get('payment_hour'):02d}:00, approved by {inv.get('approved_by')}.",
        f"Notes: {inv.get('notes')}",
    ]
    if cap:
        parts.append(
            f"Applicable policy {cap['rule_id']}: {cap.get('category')} payments capped at "
            f"${cap.get('max_amount'):,.0f}. {cap.get('text', '')}".strip()
        )
    if signals:
        parts.append("Fraud signals: " + ", ".join(signals) + ".")
    else:
        parts.append("No fraud signals detected on this invoice.")
    return " ".join(p for p in parts if p)


def build(per_file: bool = False) -> dict[str, int]:
    invoices, vendors, policies = _load()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    records = []
    for inv in invoices:
        iid = inv["invoice_id"]
        records.append({
            "invoice_id": iid,
            "title": f"{iid} — {inv.get('vendor_name')} {inv.get('amount')} {inv.get('currency', 'USD')}",
            "vendor_name": inv.get("vendor_name"),
            "department": inv.get("department"),
            "category": inv.get("category"),
            "amount": inv.get("amount"),
            "invoice_date": inv.get("invoice_date"),
            "fraud_label": inv.get("fraud_label", 0),
            "content": _content(inv, vendors, policies),
        })

    # JSONL
    (OUT_DIR / "evidence.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    )

    # CSV
    cols = ["invoice_id", "title", "vendor_name", "department", "category",
            "amount", "invoice_date", "fraud_label", "content"]
    with (OUT_DIR / "evidence.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(records)

    # optional: one markdown document per invoice
    if per_file:
        docs = OUT_DIR / "docs"
        docs.mkdir(exist_ok=True)
        for r in records:
            (docs / f"{r['invoice_id']}.md").write_text(
                f"# {r['title']}\n\ninvoice_id: {r['invoice_id']}\n\n{r['content']}\n"
            )

    return {"invoices": len(records), "vendors": len(vendors), "policies": len(policies)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-file", action="store_true", help="also write one .md per invoice")
    args = ap.parse_args()
    summary = build(per_file=args.per_file)
    print(f"Wrote evidence for {summary['invoices']} invoices to {OUT_DIR}")
    print(" - evidence.jsonl")
    print(" - evidence.csv")
    if args.per_file:
        print(f" - docs/<invoice_id>.md ({summary['invoices']} files)")
