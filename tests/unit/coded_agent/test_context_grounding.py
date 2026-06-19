"""Tests for Context Grounding result normalization.

Under Basic (text) ingestion, search hits return chunk text (not structured
columns), so invoice_id (a UUID) is recovered from the text. These tests cover
both the explicit-metadata path and the text-extraction fallback.
"""

from __future__ import annotations

from fraudcase_ai.coded_agent.context_grounding import _normalize

UUID_A = "44e6e272-8dfb-439b-bc8a-3a65896261d6"
UUID_B = "aa459332-5e45-456a-9a7f-a7572237d351"


def test_explicit_invoice_id_metadata_path():
    results = [{"invoice_id": "INV-1", "score": 0.91, "content": "ignored"}]
    hits = _normalize(results)
    assert hits == [{"invoice_id": "INV-1", "score": 0.91, "source": "uipath_context_grounding"}]


def test_extracts_uuid_from_chunk_text():
    results = [{"content": f"Invoice {UUID_A} from vendor Acme ...", "score": 0.88}]
    hits = _normalize(results)
    assert hits[0]["invoice_id"] == UUID_A
    assert hits[0]["score"] == 0.88


def test_multiple_invoices_in_one_chunk_dedup_keeps_first_score():
    results = [
        {"content": f"Invoice {UUID_A} ... Invoice {UUID_B} ...", "score": 0.95},
        {"content": f"Invoice {UUID_A} again ...", "score": 0.70},  # dup, lower score
    ]
    hits = _normalize(results)
    ids = [h["invoice_id"] for h in hits]
    assert ids == [UUID_A, UUID_B]  # order preserved, deduped
    assert hits[0]["score"] == 0.95


def test_handles_object_results_and_alt_score_keys():
    class R:
        def __init__(self, text, relevance):
            self.page_content = text
            self.relevance = relevance

    hits = _normalize([R(f"Invoice {UUID_B} flagged ghost vendor", 0.83)])
    assert hits == [{"invoice_id": UUID_B, "score": 0.83, "source": "uipath_context_grounding"}]


def test_empty_or_no_match_returns_empty():
    assert _normalize([]) == []
    assert _normalize([{"content": "no ids here", "score": 0.9}]) == []
