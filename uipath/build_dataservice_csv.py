"""Build per-entity CSV files for importing the demo dataset into UiPath Data Service.

Data Fabric's in-UI "Import data" maps CSV columns to entity fields by their display
names, so headers use the underscored field names (invoice_id, vendor_id, ...). Run:

    python uipath/build_dataservice_csv.py

Outputs (under uipath/dataservice_seed/): Vendor.csv, Policy.csv, Transaction.csv
Import order: Vendor and Policy first, then Transaction.
"""

from __future__ import annotations

import csv
from pathlib import Path

from fraudcase_ai.config import DATA_DIR
from fraudcase_ai.data.load import load_json_dir
from fraudcase_ai.models import Invoice, Policy, Vendor

OUT_DIR = Path(__file__).resolve().parent / "dataservice_seed"

# Columns per entity = model fields (Transaction drops the retrieval-only embedding).
TXN_COLS = [f for f in Invoice.model_fields if f != "embedding"]
VENDOR_COLS = list(Vendor.model_fields)
POLICY_COLS = list(Policy.model_fields)


def _write(name: str, rows: list[dict], cols: list[str]) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / name).open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c) for c in cols})
    return len(rows)


def main() -> None:
    data = load_json_dir(DATA_DIR)
    v = _write("Vendor.csv", data["vendors"], VENDOR_COLS)
    p = _write("Policy.csv", data["policies"], POLICY_COLS)
    t = _write("Transaction.csv", data["invoices"], TXN_COLS)
    print(f"Wrote {OUT_DIR}:")
    print(f"  Vendor.csv      ({v} rows)")
    print(f"  Policy.csv      ({p} rows)")
    print(f"  Transaction.csv ({t} rows)")
    print("Import order: Vendor + Policy first, then Transaction.")


if __name__ == "__main__":
    main()
