"""Context Grounding retrieval for the coded agent, via the UiPath Python SDK.

Uses ``uipath`` SDK's ``context_grounding.search`` when running on Automation Cloud.
The SDK is imported lazily so the package stays importable (and unit-testable) in
environments where ``uipath`` is not installed; there, retrieval falls back to the
local demo dataset scorer shared with the FastAPI demo path.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from fraudcase_ai.config import get_settings
from fraudcase_ai.uipath.clients import ContextGroundingRetriever

# A retriever is a plain callable: (objective, limit) -> list of hit dicts with
# at least ``invoice_id`` and ``score``. Keeping it a callable makes the coded
# agent trivially testable with a fake.
RetrieveFn = Callable[[str, int], list[dict[str, Any]]]

# invoice_id is a UUID in our data. Under Basic (text) ingestion of the CSV,
# Context Grounding returns chunk *text* (not structured columns), so we recover
# invoice_id(s) by matching UUIDs in the chunk content; the evidence text always
# leads with "Invoice <invoice_id> from vendor ...".
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_TEXT_KEYS = ("content", "text", "page_content", "chunk", "snippet", "value")
_SCORE_KEYS = ("score", "similarity", "relevance", "_score")


def _get(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


def _result_text(item: Any) -> str:
    for key in _TEXT_KEYS:
        value = _get(item, key)
        if isinstance(value, str) and value:
            return value
    return ""


def _result_score(item: Any) -> float:
    for key in _SCORE_KEYS:
        value = _get(item, key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return 0.0


def _normalize(results: Any) -> list[dict[str, Any]]:
    """Map UiPath Context Grounding search results to {invoice_id, score} hits.

    Prefers an explicit ``invoice_id`` (metadata/field) when present; otherwise
    extracts UUID invoice ids from the chunk text. Results are ordered by score, so
    we keep the first (highest) score seen per invoice_id.
    """
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in results or []:
        metadata = _get(item, "metadata") or {}
        meta_get = metadata.get if isinstance(metadata, dict) else (lambda k: getattr(metadata, k, None))
        explicit = _get(item, "invoice_id") or meta_get("invoice_id")
        score = _result_score(item)
        invoice_ids = [explicit] if explicit else _UUID_RE.findall(_result_text(item))
        for iid in invoice_ids:
            if iid and iid not in seen:
                seen.add(iid)
                hits.append({"invoice_id": iid, "score": score,
                             "source": "uipath_context_grounding"})
    return hits


def sdk_search(objective: str, limit: int = 8, *, index_name: str | None = None) -> list[dict[str, Any]]:
    """Query UiPath Context Grounding via the SDK. Raises on a real platform error."""
    from uipath.platform import UiPath  # lazy: only needed at runtime on Automation Cloud

    index = index_name or get_settings().uipath_context_grounding_index_name
    sdk = UiPath()
    results = sdk.context_grounding.search(name=index, query=objective, number_of_results=limit)
    return _normalize(results)


def default_retriever() -> RetrieveFn:
    """Return the retriever the coded agent uses by default.

    Prefers the UiPath SDK; if the SDK is not importable (e.g. local `uipath run`
    without the platform), uses the credential-free local fallback so the agent
    still produces evidence in development.
    """
    def retrieve(objective: str, limit: int = 8) -> list[dict[str, Any]]:
        try:
            import uipath.platform  # noqa: F401 — availability probe
        except Exception:
            return ContextGroundingRetriever()._local_retrieval_fallback(objective, limit)
        return sdk_search(objective, limit)

    return retrieve
