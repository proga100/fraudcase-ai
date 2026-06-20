# UiPath AgentHack 2026 — Alignment Plan

How FraudCase AI maps onto the AgentHack judging criteria, what is built in this
repo, and the tenant-side steps needed to make the platform usage live.

Track: **Agentic Case Management** (dynamic, exception-heavy, human-in-the-loop).

## Judging criteria → how we address them

| Criterion | How FraudCase AI addresses it |
|---|---|
| Platform Usage (depth & deliberateness) | Coded Agent (Python SDK) + Context Grounding + Data Service + **Agent Builder** (plan reasoning) + **Maestro Case** orchestration + Action Center human tasks |
| Technical Execution / Feasibility | Two human gates, recoverable Context Grounding exception, gated audit-log write, 126 passing tests |
| Completeness | Public repo + README + this plan + (to record) ≤5-min demo video |
| Creativity | Detector suite + retrieval evidence fused into auditor-reviewed findings |
| Coding-agent bonus | Built with **Claude Code** connected to UiPath (`uip` CLI + skills) |

## Architecture

```
UiPath Maestro Case (BPMN, Automation Cloud)
  ├─ service task ─────────────▶ coded agent: plan(objective)            ── Agent Builder authors the plan
  ├─ human task (Action Center) ▶ Gate 1: auditor approves/edits plan
  ├─ service task ─────────────▶ coded agent: investigate(objective, plan)── Data Service read + Context Grounding search + detectors
  ├─ human task (Action Center) ▶ Gate 2: auditor approves/rejects findings
  └─ service task ─────────────▶ coded agent: finalize(case_id, …)        ── gated write of findings + audit log to Data Service
```

The coded agent runs request/response on Automation Cloud Serverless Robots, so the
long-running human-in-the-loop flow is split into three entrypoints Maestro calls
between its human tasks. Source: [`fraudcase_ai/coded_agent/`](../fraudcase_ai/coded_agent/),
mapped in [`uipath.json`](../uipath.json).

The FastAPI app ([`fraudcase_ai/server/`](../fraudcase_ai/server/)) remains the local
demo + web UI and shares the same tools, so behaviour matches the coded agent.

## Phase status

| Phase | What | Where | Status |
|---|---|---|---|
| 0 | UiPath-first migration (Data Service + Context Grounding; no Mongo/Gemini) | whole repo | ✅ done |
| 1 | Connect Claude Code to UiPath (`uip` CLI + skills) — coding-agent bonus | tenant + dev machine | ⬜ you |
| 2 | Repackage as a UiPath **Coded Agent** (Python SDK, 3 entrypoints) | `coded_agent/`, `uipath.json` | ✅ done (publish = you) |
| 3 | **Agent Builder** agent authors the plan | `plan()` hook + tenant | ◑ hook done, agent = you |
| 4 | **Maestro Case** process calling the agent via API Workflows | tenant | ⬜ you |
| 5 | README + ≤5-min demo video | repo | ⬜ you |

## Phase 1 — Coding-agent bonus (`uip` CLI + skills)

```bash
# install the coding-agents CLI and authenticate to your tenant
#   (see docs.uipath.com/uipath-cli → "Using UiPath CLI with Coding Agents")
uip auth
uip skills install        # installs UiPath skill bundles so Claude Code can build/operate UiPath
```

Then build/iterate from Claude Code with those skills active, and show it in the demo.

## Phase 2 — Build & deploy the coded agent (UiPath Python SDK)

```bash
pip install -r requirements.txt        # includes `uipath`

uipath auth                            # browser login; writes tenant creds to .env
uipath init                            # discovers entrypoints from uipath.json -> entry-points.json + bindings.json

# run each step locally (uses local demo fallback when UiPath creds/SDK calls are absent)
uipath run plan '{"objective": "Audit this month vendor payments"}'
uipath run investigate '{"objective": "Audit this month vendor payments", "plan": "..."}'
uipath run finalize '{"case_id": "c1", "objective": "Audit...", "approved_ids": ["INV-1"], "items": [ ... ]}'

uipath pack                            # builds the .nupkg
uipath publish                         # uploads to Orchestrator -> create a process from it
```

Entrypoints (see [`coded_agent/main.py`](../fraudcase_ai/coded_agent/main.py)):
- `plan(objective)` → `{plan, plan_source}`
- `investigate(objective, plan)` → `{items, total_flagged, total_at_risk, exceptions}`
- `finalize(case_id, objective, approved_ids, items)` → `{report, audit_log, flagged_count}`

## Phase 3 — Agent Builder for plan reasoning

1. In **Agent Builder**, create an agent named `Audit Plan Agent` taking `objective`
   (String) and returning `plan` (String). Publish it (it becomes an Orchestrator
   process/release).
2. Point the coded agent at it:
   ```env
   UIPATH_PLAN_AGENT_NAME=Audit Plan Agent
   UIPATH_PLAN_AGENT_FOLDER=Shared      # the Orchestrator folder it's published in
   ```
3. `plan()` calls it synchronously via `sdk.processes.invoke(name, input_arguments=
   {"objective": ...}, folder_path=...)` and reads `OutputArguments.plan`
   (`_invoke_plan_agent` in [`coded_agent/main.py`](../fraudcase_ai/coded_agent/main.py)),
   falling back to the deterministic planner if it is unset or fails. Verify the folder
   path at runtime — the published folder must be reachable from the coded agent's context.

## Phase 4 — Maestro Case orchestration

Build the BPMN 2.0 case in **Studio Web** (Maestro "Start modeling" → Studio Web);
the contract is in [`uipath/fraudcase-ai-openapi.yaml`](../uipath/fraudcase-ai-openapi.yaml).

Verified tenant constraints (staging `hackathon26_739`):
- **Action Center is not enabled**, so model Gate 1 / Gate 2 as BPMN **User tasks**
  (a User task suspends the instance until completed — a valid human-in-the-loop gate).
  Resume on task completion, or via a Signal/Message catch event from an approval surface.
- Invoke the agents with **Call activity / Agentic task** (no first-class HTTP/Data
  Service BPMN element; use a Service task / invoked workflow for those).
- Agents must live in a **Shared** (standard) folder to be callable by the Case —
  publish the coded agent to Shared and move "Audit Plan Agent" there too.

Case flow: Start → Agentic task `Audit Plan Agent` (or coded `plan`) → **User task (Gate 1)**
→ Call activity coded `investigate` → **User task (Gate 2)** → Call activity coded
`finalize` → End. Persist `case_id`; drive the case view from `maestro_stage`; route
Context Grounding exceptions (from `investigate`'s `exceptions`) to a review path.

## Phase 5 — Submission

- Update the README with the live architecture + screenshots.
- Record a ≤5-minute demo: Maestro opens a case → plan (Agent Builder) → Gate 1 →
  investigate (Data Service + Context Grounding) → Gate 2 → finalize (audit-log
  write) → report; include the exception path.
