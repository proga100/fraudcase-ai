"""UiPath Data Service and Context Grounding clients.

Production intent:
    * UiPath Data Service stores transactions, vendors, policies, cases, and audit logs.
    * UiPath Context Grounding owns indexing, embedding generation, vector storage, and
      retrieval over audit evidence.

Local fallback:
    When UiPath endpoints are not configured, these clients read the demo dataset and
    use deterministic text matching. That keeps development and tests credential-free
    while preserving the same runner contract.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

from fraudcase_ai.config import DATA_DIR, Settings, get_settings
from fraudcase_ai.data.load import load_json_dir
from fraudcase_ai.models import FlaggedItem, Invoice, Policy, Vendor


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    """Normalize common API response envelopes to a list of records."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("value", "items", "data", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def _canon(name: str) -> str:
    """Underscore-insensitive, case-insensitive key for matching field names.

    UiPath Data Service strips underscores from field display names to form the
    stored Name (``invoice_id`` -> ``invoiceid``), so we match on this canonical
    form in both directions.
    """
    return name.replace("_", "").lower()


def _remap_to_model(records: list[dict[str, Any]], model: type) -> list[dict[str, Any]]:
    """Map UiPath Data Service records (underscore-stripped keys) to model fields."""
    fields = list(model.model_fields)
    mapped: list[dict[str, Any]] = []
    for record in records:
        norm = {_canon(str(k)): v for k, v in record.items()}
        mapped.append({f: norm[_canon(f)] for f in fields if _canon(f) in norm})
    return mapped


@dataclass(frozen=True)
class UiPathToken:
    access_token: str
    token_type: str = "Bearer"

    @property
    def authorization_header(self) -> str:
        return f"{self.token_type} {self.access_token}".strip()


class UiPathAuth:
    """Client-credentials auth helper for UiPath external application calls."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._token: UiPathToken | None = None

    async def token(self) -> UiPathToken | None:
        s = self._settings
        if self._token is not None:
            return self._token
        if not (s.uipath_oauth_token_url and s.uipath_client_id and s.uipath_client_secret):
            return None
        data = {
            "grant_type": "client_credentials",
            "client_id": s.uipath_client_id,
            "client_secret": s.uipath_client_secret,
        }
        if s.uipath_scope:
            data["scope"] = s.uipath_scope
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(s.uipath_oauth_token_url, data=data)
            response.raise_for_status()
            body = response.json()
        self._token = UiPathToken(
            access_token=str(body["access_token"]),
            token_type=str(body.get("token_type") or "Bearer"),
        )
        return self._token


class DataServiceStore:
    """Read/write facade for UiPath Data Service entities."""

    def __init__(self, settings: Settings | None = None, auth: UiPathAuth | None = None) -> None:
        self._settings = settings or get_settings()
        self._auth = auth or UiPathAuth(self._settings)

    @property
    def configured(self) -> bool:
        return bool(
            self._settings.uipath_dataservice_transactions_url
            and self._settings.uipath_dataservice_vendors_url
            and self._settings.uipath_dataservice_policies_url
        )

    async def load_case_dataset(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Return transactions, vendors, and policies from UiPath Data Service.

        Falls back to the bundled demo dataset if Data Service is not configured or a
        read fails (e.g. the tenant blocks the caller from entity records) so the
        agent degrades gracefully instead of crashing.
        """
        if self.configured:
            try:
                transactions, vendors, policies = await asyncio.gather(
                    self._get_records(self._settings.uipath_dataservice_transactions_url),
                    self._get_records(self._settings.uipath_dataservice_vendors_url),
                    self._get_records(self._settings.uipath_dataservice_policies_url),
                )
                # Data Service stores underscore-stripped field Names; map back to the
                # model field names the detector suite expects.
                return (
                    _remap_to_model(transactions, Invoice),
                    _remap_to_model(vendors, Vendor),
                    _remap_to_model(policies, Policy),
                )
            except Exception as exc:  # noqa: BLE001 — degrade to the bundled dataset
                log.warning("Data Service read failed (%s); using bundled demo dataset",
                            type(exc).__name__)
        data = load_json_dir(DATA_DIR)
        return data["invoices"], data["vendors"], data["policies"]

    async def write_audit_log(
        self,
        case_id: str,
        objective: str,
        approved_items: list[FlaggedItem],
    ) -> dict[str, Any]:
        """Append the Gate-2 approved audit outcome to the UiPath Data Service AuditLog."""
        invoice_ids = [item.invoice_id for item in approved_items]
        total_at_risk = sum(item.amount for item in approved_items)
        # Canonical record we return to callers (underscored, model-friendly).
        payload = {
            "case_id": case_id,
            "case_objective": objective,
            "invoice_ids": invoice_ids,
            "count": len(approved_items),
            "total_at_risk": total_at_risk,
            "approved_findings": [item.model_dump(mode="json") for item in approved_items],
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": "uipath_data_service",
        }
        url = self._settings.uipath_dataservice_audit_log_url
        if url:
            # The AuditLog entity uses underscore-stripped field Names and only the
            # scalar fields it defines; invoice_ids is a Text field (comma-joined).
            wire = {
                "caseid": case_id,
                "caseobjective": objective,
                "invoiceids": ",".join(invoice_ids),
                "count": len(approved_items),
                "totalatrisk": total_at_risk,
                "source": "uipath_data_service",
            }
            await self._post(url, wire)
        return payload

    async def _get_records(self, url: str) -> list[dict[str, Any]]:
        token = await self._auth.token()
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = token.authorization_header
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return _extract_records(response.json())

    async def _post(self, url: str, payload: dict[str, Any]) -> None:
        token = await self._auth.token()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = token.authorization_header
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()


class ContextGroundingRetriever:
    """Retriever for UiPath Context Grounding evidence."""

    def __init__(self, settings: Settings | None = None, auth: UiPathAuth | None = None) -> None:
        self._settings = settings or get_settings()
        self._auth = auth or UiPathAuth(self._settings)

    @property
    def configured(self) -> bool:
        return bool(self._settings.uipath_context_grounding_query_url)

    @property
    def index_name(self) -> str:
        return self._settings.uipath_context_grounding_index_name

    async def query(self, objective: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """Return invoice evidence ranked by UiPath Context Grounding.

        The configured URL is expected to be a UiPath API Workflow/HTTP endpoint that
        queries a Context Grounding index and returns records with ``invoice_id`` plus
        an optional ``score``. Embeddings are created and stored by UiPath, not by this
        Python service.
        """
        if self.configured:
            return await self._query_uipath(objective, limit)
        return self._local_retrieval_fallback(objective, limit)

    async def _query_uipath(self, objective: str, limit: int) -> list[dict[str, Any]]:
        token = await self._auth.token()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = token.authorization_header
        payload = {
            "query": objective,
            "index_name": self._settings.uipath_context_grounding_index_name,
            "limit": limit,
        }
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                self._settings.uipath_context_grounding_query_url,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            return _extract_records(response.json())

    def _local_retrieval_fallback(self, objective: str, limit: int) -> list[dict[str, Any]]:
        tokens = {t.strip(".,:;!?()[]{}").lower() for t in objective.split() if len(t) > 2}
        data = load_json_dir(DATA_DIR)
        scored: list[dict[str, Any]] = []
        for invoice in data["invoices"]:
            text = " ".join(
                str(invoice.get(field, ""))
                for field in ("invoice_id", "vendor_name", "department", "category", "notes", "embedding_text")
            ).lower()
            overlap = sum(1 for token in tokens if token in text)
            bonus = int(bool(invoice.get("is_fraud_exemplar"))) + int(bool(invoice.get("is_duplicate")))
            raw_score = overlap + bonus
            if raw_score <= 0:
                continue
            scored.append({
                "invoice_id": invoice["invoice_id"],
                "score": min(0.99, 0.80 + raw_score / 100),
                "source": "local_context_grounding_fallback",
            })
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:limit]
