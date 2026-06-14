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
        assert payload["count"] == 2
        assert payload["total_at_risk"] == 350.0
        sent = json.loads(route.calls.last.request.content)
        assert sent["case_id"] == "case-1"
        assert sent["invoice_ids"] == ["INV-1", "INV-2"]
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
