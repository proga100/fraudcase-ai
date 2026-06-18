"""Agent roster — attribution for each stage of the audit pipeline.

Every AgentEvent carries this attribution so the UI can show *which agent* did
*what* with *which tool*. Both runners (FakeRunner and UiPathRunner) import from
here so the scripted demo and the live case tell the same story.

Labels are honest about the UiPath-first implementation: structured records are
read from and written to UiPath Data Service; semantic evidence retrieval (with
embeddings and vector indexing owned entirely by UiPath) runs through UiPath
Context Grounding; the policy / duplicate / ghost-vendor / off-hours detectors are
internal tools operating on Data Service records; OFAC is a live US Treasury feed;
the plan and report narrative are produced deterministically by the coded agent.
"""

from __future__ import annotations

MISSION_PLANNING = {"agent": "Audit Planning Agent", "tool_label": "Deterministic audit planner"}
DATA_SERVICE_READ = {"agent": "Data Service Agent", "tool_label": "UiPath Data Service · read"}
VECTOR_SEARCH = {"agent": "Evidence Retrieval Agent", "tool_label": "UiPath Context Grounding"}
SPEND_ANALYSIS = {"agent": "Spend Analysis Agent", "tool_label": "UiPath Data Service · aggregate"}
RISK_TRIAGE = {"agent": "Risk Triage Agent", "tool_label": "Policy · Duplicate · Ghost · OFAC detectors"}
HUMAN_GATE = {"agent": "Maestro Human Approval Agent", "tool_label": "Auditor decision required"}
AUDIT_TRAIL = {"agent": "Audit Trail Agent", "tool_label": "UiPath Data Service · gated write"}
REPORT_GENERATION = {"agent": "Report Generation Agent", "tool_label": "Deterministic report builder"}
AUDIT_ASSISTANT = {"agent": "AI Audit Assistant", "tool_label": "Grounded audit assistant"}
