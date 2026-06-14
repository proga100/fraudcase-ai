"""Risk triage — assemble the flagged-candidate list from the detector suite.

Shared by the deterministic demo runner and the live external coded agent path,
so every execution path proposes the same evidence-backed candidates.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from fraudcase_ai.models import FlaggedItem, FlaggedReason, Invoice, Policy, Vendor
from fraudcase_ai.tools.dedup import find_exact_duplicates
from fraudcase_ai.tools.policy import check_policy

BUSINESS_START, BUSINESS_END = 8, 18
VECTOR_SIMILAR_MIN = 0.80  # surface vector hits at/above this score as VECTOR_SIMILAR
HIGH_SIGNAL = {
    FlaggedReason.OFAC_HIT,
    FlaggedReason.GHOST_VENDOR,
    FlaggedReason.DUPLICATE,
    FlaggedReason.NEAR_DUPLICATE,
    FlaggedReason.VECTOR_SIMILAR,
}


def assemble_flagged_records(
    invoices_raw: list[dict[str, Any]],
    vendors_raw: list[dict[str, Any]],
    policies_raw: list[dict[str, Any]],
    retrieval_hits: list[dict[str, Any]],
) -> list[FlaggedItem]:
    """Run detector suite over UiPath Data Service records.

    Context Grounding supplies retrieval hits with an ``invoice_id`` and optional
    ``score``. The application does not create embeddings directly in this path;
    UiPath owns indexing, embedding generation, and retrieval.
    """
    invoices = [Invoice.model_validate(d) for d in invoices_raw]
    vendors = {v["vendor_id"]: Vendor.model_validate(v) for v in vendors_raw}
    policies = [Policy.model_validate(p) for p in policies_raw]
    by_id = {i.invoice_id: i for i in invoices}

    reasons: dict[str, set[FlaggedReason]] = defaultdict(set)
    details: dict[str, list[str]] = defaultdict(list)
    sims: dict[str, float] = {}

    for inv in invoices:
        for v in check_policy(inv, policies):
            reasons[inv.invoice_id].add(FlaggedReason.POLICY_VIOLATION)
            details[inv.invoice_id].append(f"{v.rule_id}: {v.amount:.0f} > {v.max_amount:.0f}")
        vend = vendors.get(inv.vendor_id)
        if vend and vend.is_ghost:
            reasons[inv.invoice_id].add(FlaggedReason.GHOST_VENDOR)
            details[inv.invoice_id].append("payment to ghost vendor")
        if inv.payment_hour < BUSINESS_START or inv.payment_hour > BUSINESS_END:
            reasons[inv.invoice_id].add(FlaggedReason.OFF_HOURS)
            details[inv.invoice_id].append(f"paid at {inv.payment_hour:02d}:00")

    for original_id, dup_id in find_exact_duplicates(invoices):
        reasons[dup_id].add(FlaggedReason.DUPLICATE)
        details[dup_id].append(f"exact duplicate of {original_id}")

    for hit in retrieval_hits:
        score = float(hit.get("score") or hit.get("similarity") or 0.0)
        invoice_id = hit.get("invoice_id") or hit.get("id")
        if invoice_id and score >= VECTOR_SIMILAR_MIN:
            reasons[invoice_id].add(FlaggedReason.VECTOR_SIMILAR)
            sims[invoice_id] = score
            details[invoice_id].append(f"matched UiPath Context Grounding evidence ({score:.2f})")

    items: list[FlaggedItem] = []
    for iid, rset in reasons.items():
        inv = by_id.get(iid)
        if inv is None:
            continue
        items.append(FlaggedItem(
            invoice_id=iid,
            vendor_name=inv.vendor_name,
            department=inv.department,
            amount=inv.amount,
            reasons=sorted(rset, key=lambda r: r.value),
            similarity=sims.get(iid),
            detail="; ".join(details[iid]),
        ))

    def _priority(it: FlaggedItem):
        high = len(set(it.reasons) & HIGH_SIGNAL)
        return (high > 0, high, len(it.reasons), it.amount)

    items.sort(key=_priority, reverse=True)
    return items
