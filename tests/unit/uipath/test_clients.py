"""Tests for the UiPath Data Service + Context Grounding clients.

Covers response-envelope parsing, the credential-free local fallback, and the
configured HTTP paths (mocked with respx — no real UiPath calls).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from fraudcase_ai.config import Settings
from fraudcase_ai.models import FlaggedItem, FlaggedReason
from fraudcase_ai.uipath.clients import (
    ContextGroundingRetriever,
    DataServiceStore,
    _extract_records,
)


# --------------------------------------------------------------------------- #
# _extract_records — Data Service / Context Grounding response parsing
# --------------------------------------------------------------------------- #

class TestExtractRecords:
    def test_plain_list(self):
        assert _extract_records([{"a": 1}, {"b": 2}]) == [{"a": 1}, {"b": 2}]

    @pytest.mark.parametrize("key", ["value", "items", "data", "records"])
    def test_envelope_keys(self, key):
        assert _extract_records({key: [{"x": 1}]}) == [{"x": 1}]

    def test_filters_non_dict_items(self):
        assert _extract_records([{"a": 1}, 5, "x", None]) == [{"a": 1}]

    def test_unknown_shapes_return_empty(self):
        assert _extract_records("nope") == []
        assert _extract_records({"value": "bad"}) == []
        assert _extract_records(42) == []


# --------------------------------------------------------------------------- #
# DataServiceStore
# --------------------------------------------------------------------------- #

class TestDataServiceStore:
    @pytest.mark.anyio
    async def test_local_fallback_when_unconfigured(self):
        store = DataServiceStore(settings=Settings())
        assert store.configured is False
        txns, vendors, policies = await store.load_case_dataset()
        assert len(txns) > 0 and len(vendors) > 0 and len(policies) > 0

    @pytest.mark.anyio
    @respx.mock
    async def test_configured_reads_parse_envelopes(self):
        s = Settings(
            uipath_dataservice_transactions_url="https://ds.test/Transaction",
            uipath_dataservice_vendors_url="https://ds.test/Vendor",
            uipath_dataservice_policies_url="https://ds.test/Policy",
        )
        respx.get("https://ds.test/Transaction").mock(
            return_value=httpx.Response(200, json={"value": [{"invoice_id": "INV-1"}]})
        )
        respx.get("https://ds.test/Vendor").mock(
            return_value=httpx.Response(200, json=[{"vendor_id": "v1"}])
        )
        respx.get("https://ds.test/Policy").mock(
            return_value=httpx.Response(200, json={"items": [{"rule_id": "P1"}]})
        )
        store = DataServiceStore(settings=s)
        assert store.configured is True
        txns, vendors, policies = await store.load_case_dataset()
        assert txns == [{"invoice_id": "INV-1"}]
        assert vendors == [{"vendor_id": "v1"}]
        assert policies == [{"rule_id": "P1"}]

    @pytest.mark.anyio
    @respx.mock
    async def test_reads_map_underscore_stripped_field_names(self):
        """UiPath stores `invoice_id` as `invoiceid`; reads must map back to model fields."""
        s = Settings(
            uipath_dataservice_transactions_url="https://ds.test/Transaction",
            uipath_dataservice_vendors_url="https://ds.test/Vendor",
            uipath_dataservice_policies_url="https://ds.test/Policy",
        )
        respx.get("https://ds.test/Transaction").mock(return_value=httpx.Response(200, json={"value": [{
            "invoiceid": "INV-1", "vendorid": "v1", "vendorname": "Acme", "department": "IT",
            "category": "Software", "amount": 9000.0, "paymentmethod": "ACH",
            "invoicedate": "2026-01-01", "paymenthour": 3, "approvedby": "A", "notes": "n",
            "isghostvendor": False,
        }]}))
        respx.get("https://ds.test/Vendor").mock(return_value=httpx.Response(200, json=[{
            "vendorid": "v1", "vendorname": "Acme", "country": "USA", "category": "Software",
            "onboarded": "2022-01-01", "isghost": False, "riskscore": 0.1,
        }]))
        respx.get("https://ds.test/Policy").mock(return_value=httpx.Response(200, json=[{
            "ruleid": "P1", "category": "*", "maxamount": 100000.0, "text": "CFO approval",
        }]))
        store = DataServiceStore(settings=s)
        txns, vendors, policies = await store.load_case_dataset()
        # Keys are remapped to the model field names (with underscores).
        assert txns[0]["invoice_id"] == "INV-1"
        assert txns[0]["vendor_id"] == "v1"
        assert txns[0]["payment_hour"] == 3
        assert vendors[0]["vendor_id"] == "v1"
        assert policies[0]["rule_id"] == "P1"
        # And the remapped dicts validate cleanly against the models.
        from fraudcase_ai.models import Invoice, Policy, Vendor
        Invoice.model_validate(txns[0])
        Vendor.model_validate(vendors[0])
        Policy.model_validate(policies[0])

    @pytest.mark.anyio
    @respx.mock
    async def test_write_audit_log_posts_payload(self):
        s = Settings(uipath_dataservice_audit_log_url="https://ds.test/AuditLog")
        route = respx.post("https://ds.test/AuditLog").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        store = DataServiceStore(settings=s)
        items = [
            FlaggedItem(invoice_id="INV-1", vendor_name="Acme", department="IT",
                        amount=100.0, reasons=[FlaggedReason.DUPLICATE]),
            FlaggedItem(invoice_id="INV-2", vendor_name="Ghost", department="Ops",
                        amount=250.0, reasons=[FlaggedReason.GHOST_VENDOR]),
        ]
        payload = await store.write_audit_log("case-1", "audit objective", items)

        assert route.called
        # Returned payload keeps the canonical underscored shape.
        assert payload["count"] == 2
        assert payload["total_at_risk"] == 350.0
        assert payload["invoice_ids"] == ["INV-1", "INV-2"]
        # Wire body uses UiPath's underscore-stripped field Names; invoice_ids is a
        # comma-joined Text field.
        sent = json.loads(route.calls.last.request.content)
        assert sent["caseid"] == "case-1"
        assert sent["invoiceids"] == "INV-1,INV-2"
        assert sent["totalatrisk"] == 350.0
        assert sent["source"] == "uipath_data_service"

    @pytest.mark.anyio
    async def test_write_audit_log_no_url_returns_payload_without_post(self):
        store = DataServiceStore(settings=Settings())
        items = [FlaggedItem(invoice_id="INV-9", vendor_name="X", department="IT",
                             amount=10.0, reasons=[FlaggedReason.OFF_HOURS])]
        payload = await store.write_audit_log("case-2", "obj", items)
        assert payload["case_id"] == "case-2"
        assert payload["invoice_ids"] == ["INV-9"]


# --------------------------------------------------------------------------- #
# ContextGroundingRetriever
# --------------------------------------------------------------------------- #

class TestContextGroundingRetriever:
    @pytest.mark.anyio
    @respx.mock
    async def test_configured_parses_response(self):
        s = Settings(uipath_context_grounding_query_url="https://cg.test/query")
        route = respx.post("https://cg.test/query").mock(
            return_value=httpx.Response(200, json={"value": [{"invoice_id": "INV-1", "score": 0.91}]})
        )
        retriever = ContextGroundingRetriever(settings=s)
        assert retriever.configured is True
        hits = await retriever.query("find duplicate invoices", limit=5)
        assert hits == [{"invoice_id": "INV-1", "score": 0.91}]
        sent = json.loads(route.calls.last.request.content)
        assert sent["query"] == "find duplicate invoices"
        assert sent["limit"] == 5
        assert sent["index_name"] == retriever.index_name

    @pytest.mark.anyio
    async def test_local_fallback_scores_invoices(self):
        retriever = ContextGroundingRetriever(settings=Settings())
        assert retriever.configured is False
        hits = await retriever.query("urgent off-cycle wire to ghost vendor", limit=5)
        assert len(hits) <= 5
        assert all("invoice_id" in h and "score" in h for h in hits)
        assert all(h["source"] == "local_context_grounding_fallback" for h in hits)
