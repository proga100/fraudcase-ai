"""Context Grounding retrieval for the coded agent, via the UiPath Python SDK.

Uses ``uipath`` SDK's ``context_grounding.search`` when running on Automation Cloud.
The SDK is imported lazily so the package stays importable (and unit-testable) in
environments where ``uipath`` is not installed; there, retrieval falls back to the
local demo dataset scorer shared with the FastAPI demo path.
"""

from __future__ import annotations

from typing import Any, Callable

from fraudcase_ai.config import get_settings
from fraudcase_ai.uipath.clients import ContextGroundingRetriever

# A retriever is a plain callable: (objective, limit) -> list of hit dicts with
# at least ``invoice_id`` and ``score``. Keeping it a callable makes the coded
# agent trivially testable with a fake.
RetrieveFn = Callable[[str, int], list[dict[str, Any]]]


def _normalize(results: Any) -> list[dict[str, Any]]:
    """Map UiPath Context Grounding search results to {invoice_id, score} hits."""
    hits: list[dict[str, Any]] = []
    for item in results or []:
        get = item.get if isinstance(item, dict) else (lambda k, _i=item: getattr(_i, k, None))
        metadata = get("metadata") or {}
        meta_get = metadata.get if isinstance(metadata, dict) else (lambda k: getattr(metadata, k, None))
        invoice_id = get("invoice_id") or meta_get("invoice_id") or get("id")
        score = get("score")
        if score is None:
            score = get("similarity") or meta_get("score") or 0.0
        if invoice_id:
            hits.append({"invoice_id": invoice_id, "score": float(score or 0.0),
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
