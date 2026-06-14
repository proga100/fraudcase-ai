"""The gated WRITE. Owned by Agent-Core slice.

This is the only mutation in the system and runs ONLY after Gate-2 approval. UiPath
Maestro or the web UI submits the human decision before this write is allowed.

CONTRACT:
    mark_flagged(db, case_id, approved_ids, items) -> WriteResult
        - sets transactions.flagged=true + audit metadata on approved_ids
        - appends one audit_log document
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from fraudcase_ai.models import FlaggedItem


class WriteResult(BaseModel):
    flagged_count: int
    audit_log_id: str


def mark_flagged(
    db: Any, case_id: str, approved_ids: list[str], items: list[FlaggedItem]
) -> WriteResult:
    """Write flags for approved invoices + append an audit_log doc. Idempotent per case_id."""
    from datetime import datetime, timezone

    # Idempotency: check if audit_log already has an entry for this case_id
    existing = db.audit_log.find_one({"case_id": case_id})
    if existing is not None:
        # Already written; return the same result without double-writing
        return WriteResult(
            flagged_count=existing.get("count", 0),
            audit_log_id=case_id,
        )

    flagged_at = datetime.now(timezone.utc).isoformat()

    # Mark approved transactions as flagged
    if approved_ids:
        db.transactions.update_many(
            {"invoice_id": {"$in": approved_ids}},
            {
                "$set": {
                    "flagged": True,
                    "case_id": case_id,
                    "flagged_at": flagged_at,
                }
            },
        )

    # Append exactly ONE audit_log document for this case_id
    db.audit_log.insert_one(
        {
            "case_id": case_id,
            "invoice_ids": approved_ids,
            "count": len(approved_ids),
            "ts": flagged_at,
        }
    )

    return WriteResult(
        flagged_count=len(approved_ids),
        audit_log_id=case_id,
    )
