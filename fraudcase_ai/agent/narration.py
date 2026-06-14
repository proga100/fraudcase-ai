"""Deterministic plan + report narrative builders (no LLM).

Shared by the FastAPI demo runner and the UiPath coded agent so both tell the
same UiPath-first story. When a UiPath Agent Builder agent is configured, the
coded agent uses it for the plan instead of `build_plan`; this module is the
credential-free fallback and the narrative writer.
"""

from __future__ import annotations


def build_plan(objective: str) -> str:
    """Deterministic audit plan mirroring the live tool sequence."""
    return (
        f"Audit objective: {objective}\n"
        "1. Read transactions, vendors, and policies from UiPath Data Service.\n"
        "2. Retrieve semantically relevant invoice evidence from UiPath Context Grounding.\n"
        "3. Run deterministic checks: duplicate, policy, ghost vendor, off-hours, sanctions.\n"
        "4. Aggregate spend by department.\n"
        "5. Propose a flagged exception list for your approval.\n"
        "6. Write approved findings and the audit log back to UiPath Data Service."
    )


def build_narrative(objective: str, flagged: int, at_risk: float, reasons: list[str]) -> str:
    """Deterministic report narrative."""
    risk_types = len(set(reasons))
    return (
        f"Audit objective '{objective}' flagged {flagged} transactions totalling "
        f"${at_risk:,.0f} at risk across {risk_types} risk "
        f"type{'s' if risk_types != 1 else ''}. Evidence was retrieved from UiPath "
        "Context Grounding and every write was approved by a human auditor before it "
        "reached the UiPath Data Service audit log."
    )
