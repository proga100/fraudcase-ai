"""FraudCase AI coded-agent entrypoints (mapped in uipath.json).

Three request/response steps a UiPath Maestro Case orchestrates between its human
tasks. Every external call goes through UiPath: Context Grounding for evidence
(via the SDK) and Data Service for records + the audit-log write.

    plan(objective)                         -> { plan }            (Gate 1 review)
    investigate(objective, plan)            -> { items, totals }   (Gate 2 review)
    finalize(case_id, objective, ids, items)-> { report }          (gated write)
"""

from __future__ import annotations

import asyncio
from typing import Any

from fraudcase_ai.agent.narration import build_narrative, build_plan
from fraudcase_ai.agent.report import render_report
from fraudcase_ai.coded_agent.context_grounding import RetrieveFn, default_retriever
from fraudcase_ai.config import get_settings
from fraudcase_ai.models import FlaggedItem
from fraudcase_ai.tools.triage import assemble_flagged_records
from fraudcase_ai.uipath.clients import DataServiceStore

MAX_FLAGGED = 40


# --------------------------------------------------------------------------- #
# Phase 3 hook: reasoning via a UiPath Agent Builder agent
# --------------------------------------------------------------------------- #

def _extract_plan(result: Any) -> str | None:
    """Pull the ``plan`` text out of an Orchestrator job result (dict or object)."""
    if result is None:
        return None
    args: Any = result
    for attr in ("output_arguments", "outputArguments", "output"):
        value = result.get(attr) if isinstance(result, dict) else getattr(result, attr, None)
        if value:
            args = value
            break
    if isinstance(args, dict):
        text = args.get("plan") or args.get("output") or args.get("result")
        return str(text) if text else None
    return str(args) if args else None


def _invoke_plan_agent(agent_name: str, objective: str) -> str | None:
    """Run the Agent Builder 'Audit Plan Agent' (published as an Orchestrator process).

    Calls it synchronously via the UiPath SDK and returns OutputArguments.plan.
    Best-effort and defensive: any failure (SDK missing, agent not deployed, bad
    output) returns None so the caller falls back to the deterministic planner.
    """
    try:
        from uipath.platform import UiPath  # lazy: runtime-only

        sdk = UiPath()
        kwargs: dict[str, Any] = {"input_arguments": {"objective": objective}}
        folder = get_settings().uipath_plan_agent_folder
        if folder:
            kwargs["folder_path"] = folder
        result = sdk.processes.invoke(agent_name, **kwargs)  # type: ignore[attr-defined]
        return _extract_plan(result)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Core (dependency-injected) — unit-tested directly with fakes
# --------------------------------------------------------------------------- #

async def _plan(objective: str, *, invoke_agent=_invoke_plan_agent) -> dict[str, Any]:
    settings = get_settings()
    plan_text: str | None = None
    source = "deterministic"
    if settings.uipath_plan_agent_name:
        plan_text = await asyncio.to_thread(invoke_agent, settings.uipath_plan_agent_name, objective)
        if plan_text:
            source = "agent_builder"
    if not plan_text:
        plan_text = build_plan(objective)
    return {"objective": objective, "plan": plan_text, "plan_source": source}


async def _investigate(
    objective: str,
    plan: str,
    *,
    store: DataServiceStore,
    retrieve: RetrieveFn,
) -> dict[str, Any]:
    transactions, vendors, policies = await store.load_case_dataset()

    hits: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    try:
        hits = await asyncio.to_thread(retrieve, objective, 8)
    except Exception as exc:  # noqa: BLE001 — recoverable: route as a case exception
        exceptions.append({
            "exception_type": "context_grounding_recoverable",
            "message": (
                f"UiPath Context Grounding query failed with {type(exc).__name__}. "
                "Continuing with deterministic duplicate, policy, ghost-vendor, and "
                "off-hours checks; escalate for human review."
            ),
            "recommended_action": "Review the deterministic findings before approving Gate 2.",
        })

    all_items = assemble_flagged_records(transactions, vendors, policies, hits)
    items = all_items[:MAX_FLAGGED]
    return {
        "objective": objective,
        "plan": plan,
        "items": [i.model_dump(mode="json") for i in items],
        "total_flagged": len(all_items),
        "shown": len(items),
        "total_at_risk": sum(i.amount for i in all_items),
        "top_score": round(float(hits[0].get("score") or 0.0), 4) if hits else 0.0,
        "exceptions": exceptions,
    }


async def _finalize(
    case_id: str,
    objective: str,
    approved_ids: list[str],
    items: list[dict[str, Any]],
    *,
    store: DataServiceStore,
) -> dict[str, Any]:
    keep = set(approved_ids)
    approved = [FlaggedItem.model_validate(it) for it in items if it.get("invoice_id") in keep]

    audit = await store.write_audit_log(case_id, objective, approved)

    report = render_report(case_id, objective, approved)
    reasons = [r.value for it in approved for r in it.reasons]
    narrative = build_narrative(objective, report.flagged_count, report.total_at_risk, reasons)
    report.markdown = f"# FraudCase AI - Audit Report\n\n{narrative}\n\n" + report.markdown

    return {
        "case_id": case_id,
        "flagged_count": report.flagged_count,
        "total_at_risk": report.total_at_risk,
        "report": report.model_dump(mode="json"),
        "audit_log": audit,
    }


# --------------------------------------------------------------------------- #
# Public entrypoints (referenced by uipath.json) — clean JSON-shaped signatures
# --------------------------------------------------------------------------- #

async def plan(objective: str) -> dict[str, Any]:
    """Gate 1: author the audit plan (Agent Builder if configured, else deterministic)."""
    return await _plan(objective)


async def investigate(objective: str, plan: str = "") -> dict[str, Any]:
    """Gate 2: read Data Service + Context Grounding and propose flagged findings."""
    return await _investigate(
        objective, plan, store=DataServiceStore(), retrieve=default_retriever()
    )


async def finalize(
    case_id: str,
    objective: str,
    approved_ids: list[str],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Gated write: persist approved findings + audit log to Data Service; return the report."""
    return await _finalize(case_id, objective, approved_ids, items, store=DataServiceStore())
