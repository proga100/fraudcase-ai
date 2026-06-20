"""Runtime configuration + the MOCK/UiPath switch.

Everything is built and tested with USE_MOCKS=true. When UiPath credentials and
workflow endpoints exist, set USE_MOCKS=false and supply the UiPath settings below.
The live architecture is UiPath-first: Data Service stores structured records and
Context Grounding provides retrieval over indexed evidence.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "demo_dataset"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- mock switch ---
    use_mocks: bool = True  # tests + local dev default; flip to false with real creds

    # --- UiPath Automation Cloud ---
    uipath_oauth_token_url: str = ""
    uipath_client_id: str = ""
    uipath_client_secret: str = ""
    uipath_scope: str = ""

    # Data Service entity endpoints. Keep these configurable because tenant,
    # folder, region, and generated entity URLs differ between UiPath accounts.
    uipath_dataservice_transactions_url: str = ""
    uipath_dataservice_vendors_url: str = ""
    uipath_dataservice_policies_url: str = ""
    uipath_dataservice_audit_log_url: str = ""

    # UiPath API Workflow endpoint that queries a Context Grounding index and
    # returns relevant invoice evidence. Context Grounding owns embeddings.
    uipath_context_grounding_query_url: str = ""
    uipath_context_grounding_index_name: str = "fraudcase-ai-evidence"

    # Coded-agent reasoning: the UiPath Agent Builder agent (published as an
    # Orchestrator process) that authors the audit plan. Empty name -> the coded
    # agent uses its deterministic planner. Folder is the Orchestrator folder the
    # agent is published in (path form, e.g. "Shared"); empty -> SDK default context.
    uipath_plan_agent_name: str = ""
    uipath_plan_agent_folder: str = ""

    # --- OFAC ---
    ofac_sdn_url: str = "https://www.treasury.gov/ofac/downloads/sdn.csv"
    ofac_cache_path: str = str(REPO_ROOT / "sdn.csv")
    ofac_match_threshold: float = 0.85

    # --- near-duplicate detection ---
    neardup_similarity_threshold: float = 0.92


@lru_cache
def get_settings() -> Settings:
    return Settings()
