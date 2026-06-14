"""JSON loading for FraudCase AI's local/demo dataset.

The live system of record is UiPath Data Service; this loader only backs the
credential-free local fallback (used by the UiPath clients and tests). Embedding
generation and vector indexing are owned by UiPath Context Grounding, not by this
service, so no embedder or database client lives here anymore.
"""

from __future__ import annotations

import json
from pathlib import Path

from fraudcase_ai.models import Invoice, Policy, Vendor


# --------------------------------------------------------------------------- #
# 1.  JSON loading
# --------------------------------------------------------------------------- #

def load_json_dir(path: str | Path) -> dict[str, list[dict]]:
    """Read the four JSON files from *path* and return a dict of raw lists.

    Keys: "invoices", "vendors", "policies", "budgets".

    Raises FileNotFoundError if any of the required files is missing.
    The returned dicts are validated against the Pydantic models so callers
    get a clean failure rather than silent bad data.
    """
    base = Path(path)
    result: dict[str, list[dict]] = {}

    # --- invoices ---
    raw_invoices: list[dict] = json.loads((base / "invoices.json").read_text())
    result["invoices"] = [Invoice.model_validate(r).model_dump(mode="json") for r in raw_invoices]

    # --- vendors ---
    raw_vendors: list[dict] = json.loads((base / "vendors.json").read_text())
    result["vendors"] = [Vendor.model_validate(r).model_dump(mode="json") for r in raw_vendors]

    # --- policies ---
    raw_policies: list[dict] = json.loads((base / "policies.json").read_text())
    result["policies"] = [Policy.model_validate(r).model_dump(mode="json") for r in raw_policies]

    # --- budgets (free-form dict, no model) ---
    raw_budgets: dict = json.loads((base / "budgets.json").read_text())
    # budgets.json is {"budgets": {...}}, store as a list-of-one so callers
    # get a consistent dict[str, list] shape.
    result["budgets"] = [raw_budgets] if isinstance(raw_budgets, dict) else raw_budgets

    return result
