"""Tests for the UiPath coded-agent entrypoints (plan / investigate / finalize).

Exercises the dependency-injected core functions with fake UiPath clients — no
SDK, no tenant, no network — plus one smoke test of a public entrypoint on the
local fallback path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fraudcase_ai.coded_agent import main as ca
from fraudcase_ai.coded_agent.main import _extract_plan, _finalize, _investigate, _plan


class FakeStore:
    def __init__(self, records):
        self._records = records
        self.written: list[dict] = []

    async def load_case_dataset(self):
        return self._records

    async def write_audit_log(self, case_id, objective, approved):
        payload = {
            "case_id": case_id,
            "invoice_ids": [i.invoice_id for i in approved],
            "count": len(approved),
        }
        self.written.append(payload)
        return payload


def fake_retrieve(objective, limit=8):
    return [{"invoice_id": "i3", "score": 0.95}]


def boom_retrieve(objective, limit=8):
    raise RuntimeError("context grounding down")


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_plan_deterministic_when_no_agent_configured(monkeypatch):
    monkeypatch.setattr(ca, "get_settings", lambda: SimpleNamespace(uipath_plan_agent_name=""))
    out = await _plan("Audit vendor payments")
    assert out["plan_source"] == "deterministic"
    assert "UiPath Data Service" in out["plan"]


@pytest.mark.anyio
async def test_plan_uses_agent_builder_when_configured(monkeypatch):
    monkeypatch.setattr(ca, "get_settings", lambda: SimpleNamespace(uipath_plan_agent_name="Audit Plan Agent"))
    out = await _plan("Audit vendor payments", invoke_agent=lambda name, obj: f"AGENT PLAN: {obj}")
    assert out["plan_source"] == "agent_builder"
    assert out["plan"].startswith("AGENT PLAN")


@pytest.mark.anyio
async def test_plan_falls_back_when_agent_returns_nothing(monkeypatch):
    monkeypatch.setattr(ca, "get_settings", lambda: SimpleNamespace(uipath_plan_agent_name="Audit Plan Agent"))
    out = await _plan("Audit vendor payments", invoke_agent=lambda name, obj: None)
    assert out["plan_source"] == "deterministic"


def test_extract_plan_from_job_output_shapes():
    # dict with output_arguments (Orchestrator job result shape)
    assert _extract_plan({"output_arguments": {"plan": "P1"}}) == "P1"
    # plain dict
    assert _extract_plan({"plan": "P2"}) == "P2"
    # object with attribute
    class Job:
        output_arguments = {"plan": "P3"}
    assert _extract_plan(Job()) == "P3"
    # nothing usable
    assert _extract_plan(None) is None
    assert _extract_plan({"output_arguments": {}}) is None


# --------------------------------------------------------------------------- #
# investigate
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_investigate_flags_planted_fraud(records):
    store = FakeStore(records)
    out = await _investigate("Audit payments", "plan", store=store, retrieve=fake_retrieve)
    ids = {it["invoice_id"] for it in out["items"]}
    assert {"i2", "i4", "i5", "i6", "i3"}.issubset(ids)
    assert out["exceptions"] == []
    assert out["total_at_risk"] > 0
    assert out["top_score"] == 0.95


@pytest.mark.anyio
async def test_investigate_routes_recoverable_context_grounding_exception(records):
    store = FakeStore(records)
    out = await _investigate("Audit payments", "plan", store=store, retrieve=boom_retrieve)
    assert len(out["exceptions"]) == 1
    assert out["exceptions"][0]["exception_type"] == "context_grounding_recoverable"
    # Deterministic detectors still fire without the retrieval hit.
    ids = {it["invoice_id"] for it in out["items"]}
    assert {"i2", "i4", "i5", "i6"}.issubset(ids)


# --------------------------------------------------------------------------- #
# finalize
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_finalize_writes_only_approved_and_builds_report(records):
    store = FakeStore(records)
    inv = await _investigate("Audit payments", "plan", store=store, retrieve=fake_retrieve)
    items = inv["items"]
    approved_ids = [it["invoice_id"] for it in items if it["invoice_id"] != "i6"]

    out = await _finalize("case-1", "Audit payments", approved_ids, items, store=store)

    assert out["case_id"] == "case-1"
    assert out["flagged_count"] == len(approved_ids)
    assert store.written[0]["count"] == len(approved_ids)
    assert "i6" not in store.written[0]["invoice_ids"]
    assert "# FraudCase AI" in out["report"]["markdown"]


# --------------------------------------------------------------------------- #
# public entrypoint smoke (local fallback: demo dataset, no SDK/creds)
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_public_investigate_smoke():
    from fraudcase_ai.coded_agent import investigate
    out = await investigate("Audit this month's vendor payments")
    assert "items" in out
    assert "total_flagged" in out
    assert isinstance(out["items"], list)
