"""Unit tests for fraudcase_ai.data.load.

The loader now only reads the local/demo JSON fixtures (the credential-free
fallback behind the UiPath Data Service clients). Embedding generation and
MongoDB upsert moved to UiPath Context Grounding / Data Service and are gone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fraudcase_ai.data.load import load_json_dir
from fraudcase_ai.models import Invoice, Policy, Vendor


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def small_invoice_dict() -> dict:
    """One minimal invoice dict matching the Invoice schema."""
    return {
        "invoice_id": "test-inv-001",
        "vendor_id": "test-vendor-001",
        "vendor_name": "TestCo Ltd",
        "department": "IT",
        "category": "Software",
        "amount": 12345.67,
        "currency": "USD",
        "payment_method": "ACH",
        "invoice_date": "2026-01-15",
        "payment_hour": 10,
        "approved_by": "J. Tester",
        "notes": "Annual licence renewal",
        "is_duplicate": False,
        "is_near_duplicate": False,
        "is_policy_violation": False,
        "is_off_hours": False,
        "is_ghost_vendor": False,
        "is_fraud_exemplar": False,
        "fraud_label": 0,
        "embedding_text": "TestCo Ltd IT Software 12345.67 Annual licence renewal",
        "embedding": None,
    }


@pytest.fixture
def small_vendor_dict() -> dict:
    return {
        "vendor_id": "test-vendor-001",
        "vendor_name": "TestCo Ltd",
        "country": "USA",
        "category": "Software",
        "onboarded": "2022-06-01",
        "is_ghost": False,
        "risk_score": 0.1,
    }


@pytest.fixture
def small_policy_dict() -> dict:
    return {
        "rule_id": "P1",
        "category": "Travel",
        "max_amount": 5000.0,
        "text": "Travel > 5000 requires VP approval.",
    }


@pytest.fixture
def tmp_json_dir(small_invoice_dict, small_vendor_dict, small_policy_dict, tmp_path) -> Path:
    """Write the four JSON files to a temp directory and return the path."""
    (tmp_path / "invoices.json").write_text(json.dumps([small_invoice_dict]))
    (tmp_path / "vendors.json").write_text(json.dumps([small_vendor_dict]))
    (tmp_path / "policies.json").write_text(json.dumps([small_policy_dict]))
    (tmp_path / "budgets.json").write_text(
        json.dumps({"budgets": {"IT": 200000, "HR": 150000}})
    )
    return tmp_path


# --------------------------------------------------------------------------- #
# Tests: load_json_dir
# --------------------------------------------------------------------------- #

class TestLoadJsonDir:
    def test_returns_all_four_keys(self, tmp_json_dir):
        data = load_json_dir(tmp_json_dir)
        assert set(data.keys()) == {"invoices", "vendors", "policies", "budgets"}

    def test_invoice_parses_pydantic_fields(self, tmp_json_dir):
        data = load_json_dir(tmp_json_dir)
        assert len(data["invoices"]) == 1
        Invoice.model_validate(data["invoices"][0])

    def test_vendor_parses_pydantic_fields(self, tmp_json_dir):
        data = load_json_dir(tmp_json_dir)
        assert len(data["vendors"]) == 1
        Vendor.model_validate(data["vendors"][0])

    def test_policy_parses_pydantic_fields(self, tmp_json_dir):
        data = load_json_dir(tmp_json_dir)
        assert len(data["policies"]) == 1
        Policy.model_validate(data["policies"][0])

    def test_budgets_is_list(self, tmp_json_dir):
        data = load_json_dir(tmp_json_dir)
        assert isinstance(data["budgets"], list)
        assert len(data["budgets"]) >= 1

    def test_missing_file_raises(self, tmp_path):
        # Only write 3 of the 4 files → should raise FileNotFoundError
        (tmp_path / "invoices.json").write_text("[]")
        (tmp_path / "vendors.json").write_text("[]")
        (tmp_path / "policies.json").write_text("[]")
        # budgets.json intentionally missing
        with pytest.raises(FileNotFoundError):
            load_json_dir(tmp_path)


# --------------------------------------------------------------------------- #
# Integration: load from real demo_dataset JSONs if present
# --------------------------------------------------------------------------- #

class TestLoadFromDemoDataset:
    def test_load_real_demo_data_if_exists(self):
        """If demo_dataset JSON files exist, parse them into Pydantic models without error."""
        from fraudcase_ai.config import DATA_DIR

        invoices_file = DATA_DIR / "invoices.json"
        if not invoices_file.exists():
            pytest.skip("demo_dataset/*.json not present — run generate_data.py first")

        data = load_json_dir(DATA_DIR)
        # Should have at least 1000 invoices
        assert len(data["invoices"]) >= 1000
        # All should validate cleanly
        for inv in data["invoices"][:10]:  # sample check for speed
            Invoice.model_validate(inv)
