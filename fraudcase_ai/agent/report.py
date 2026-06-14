"""Audit report renderer. Owned by Agent-Core slice.

CONTRACT:
    render_report(case_id, case_objective, items) -> AuditReport
"""

from __future__ import annotations

from fraudcase_ai.models import AuditReport, FlaggedItem


def render_report(case_id: str, case_objective: str, items: list[FlaggedItem]) -> AuditReport:
    """Compute report totals and render a Markdown body.

    Returns an AuditReport with:
        - flagged_count: number of items
        - total_at_risk: sum of item.amount for all flagged items
        - markdown: a clean Markdown string with title, summary, and an items table
    """
    flagged_count = len(items)
    total_at_risk = sum(item.amount for item in items)

    lines: list[str] = []
    lines.append(f"# FraudCase AI — Audit Report")
    lines.append("")
    lines.append(f"**Case Objective:** {case_objective}")
    lines.append(f"**Case ID:** `{case_id}`")
    lines.append(f"**Flagged invoices:** {flagged_count}")
    lines.append(f"**Total at risk:** ${total_at_risk:,.2f}")
    lines.append("")

    if items:
        lines.append("## Flagged Items")
        lines.append("")
        lines.append(
            "| Invoice ID | Vendor | Department | Amount | Reasons | Detail |"
        )
        lines.append("|---|---|---|---|---|---|")
        for item in items:
            reasons_str = ", ".join(r.value for r in item.reasons) if item.reasons else ""
            amount_str = f"${item.amount:,.2f}"
            detail = item.detail.replace("|", "\\|")
            lines.append(
                f"| {item.invoice_id} | {item.vendor_name} | {item.department}"
                f" | {amount_str} | {reasons_str} | {detail} |"
            )
        lines.append("")
    else:
        lines.append("_No flagged items found._")
        lines.append("")

    markdown = "\n".join(lines)

    return AuditReport(
        case_id=case_id,
        case_objective=case_objective,
        flagged_count=flagged_count,
        total_at_risk=total_at_risk,
        items=items,
        markdown=markdown,
    )
