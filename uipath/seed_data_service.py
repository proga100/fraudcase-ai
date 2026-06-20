"""Seed UiPath Data Service entities from the demo dataset.

Uses the FraudCase AI external-application client credentials (from .env) to bulk
create Transaction / Vendor / Policy records in the tenant's Data Service. Field
keys are converted to UiPath's underscore-stripped Names (invoice_id -> invoiceid).

Run from the repo root after filling .env with the UIPATH_* values:

    python uipath/seed_data_service.py            # seed everything
    python uipath/seed_data_service.py --limit 25 # seed a small sample first
    python uipath/seed_data_service.py --only vendors policies

Idempotency: entities with a unique key (Transaction.invoice_id, Vendor.vendor_id,
Policy.rule_id) reject duplicates, so re-running skips already-seeded rows.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import httpx

from fraudcase_ai.config import get_settings
from fraudcase_ai.data.load import load_json_dir
from fraudcase_ai.config import DATA_DIR
from fraudcase_ai.models import Invoice, Policy, Vendor
from fraudcase_ai.uipath.clients import UiPathAuth

CONCURRENCY = 8

# Fields to send per entity (model fields minus retrieval-only columns).
TXN_FIELDS = [f for f in Invoice.model_fields if f != "embedding"]
VENDOR_FIELDS = list(Vendor.model_fields)
POLICY_FIELDS = list(Policy.model_fields)


def _to_uipath(record: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Project a record onto the entity fields, stripping underscores from keys."""
    return {f.replace("_", ""): record.get(f) for f in fields if f in record}


async def _seed_entity(
    client: httpx.AsyncClient,
    url: str,
    records: list[dict[str, Any]],
    fields: list[str],
    headers: dict[str, str],
    label: str,
) -> None:
    sem = asyncio.Semaphore(CONCURRENCY)
    created = skipped = failed = 0

    async def _post(rec: dict[str, Any]) -> None:
        nonlocal created, skipped, failed
        body = _to_uipath(rec, fields)
        async with sem:
            try:
                resp = await client.post(url, json=body, headers=headers)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  ! {label}: request error {type(exc).__name__}")
                return
        if resp.status_code in (200, 201):
            created += 1
        elif resp.status_code in (409, 412):  # duplicate unique key
            skipped += 1
        else:
            failed += 1
            if failed <= 5:
                print(f"  ! {label}: HTTP {resp.status_code} {resp.text[:160]}")

    await asyncio.gather(*(_post(r) for r in records))
    print(f"{label}: created={created} skipped(dup)={skipped} failed={failed} (of {len(records)})")


async def main(limit: int | None, only: list[str]) -> None:
    s = get_settings()
    missing = [n for n, v in {
        "UIPATH_DATASERVICE_TRANSACTIONS_URL": s.uipath_dataservice_transactions_url,
        "UIPATH_DATASERVICE_VENDORS_URL": s.uipath_dataservice_vendors_url,
        "UIPATH_DATASERVICE_POLICIES_URL": s.uipath_dataservice_policies_url,
    }.items() if not v]
    if missing:
        raise SystemExit(f"Missing .env values: {', '.join(missing)}")

    token = await UiPathAuth(s).token()
    if token is None:
        raise SystemExit("Could not get a UiPath token — check UIPATH_CLIENT_ID/SECRET/SCOPE in .env")
    headers = {
        "Authorization": token.authorization_header,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    data = load_json_dir(DATA_DIR)
    invoices = data["invoices"][:limit] if limit else data["invoices"]
    vendors = data["vendors"][:limit] if limit else data["vendors"]
    policies = data["policies"][:limit] if limit else data["policies"]

    jobs = {
        "vendors": (s.uipath_dataservice_vendors_url, vendors, VENDOR_FIELDS),
        "policies": (s.uipath_dataservice_policies_url, policies, POLICY_FIELDS),
        "transactions": (s.uipath_dataservice_transactions_url, invoices, TXN_FIELDS),
    }
    selected = only or list(jobs)

    async with httpx.AsyncClient(timeout=60) as client:
        # vendors + policies first (transactions reference vendors)
        for name in ["vendors", "policies", "transactions"]:
            if name not in selected:
                continue
            url, records, fields = jobs[name]
            await _seed_entity(client, url, records, fields, headers, name)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="seed only the first N of each")
    ap.add_argument("--only", nargs="+", default=[],
                    choices=["vendors", "policies", "transactions"],
                    help="seed only these entities")
    args = ap.parse_args()
    asyncio.run(main(args.limit, args.only))
