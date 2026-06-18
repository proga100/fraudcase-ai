# FraudCase AI

FraudCase AI is an AI-powered finance transaction fraud audit case-management workflow built for UiPath AgentHack Track 1: UiPath Maestro Case.

The app turns a plain-English audit objective into a governed audit case. An external coded agent service plans the audit, investigates transactions, vendors, policies, duplicates, OFAC risk, and suspicious payment patterns, then pauses for human decisions before any audit-log write happens.

## UiPath AgentHack Track 1 Positioning

FraudCase AI targets Track 1: UiPath Maestro Case.

UiPath Maestro Case is the visible orchestration and governance layer:

1. Audit mission becomes a Maestro audit case.
2. Gate 1 is plan review, where a human can approve, edit, or reject the audit plan.
3. Agent tools investigate transactions, vendors, policies, duplicates, OFAC exposure, and off-hours activity.
4. Suspicious findings become case exceptions.
5. Gate 2 is exception review, where a human approves or rejects flagged items.
6. Approved findings are written to the audit log.
7. The case produces an audit report and can be closed.

The implementation exposes Maestro-oriented adapter endpoints so a Maestro Case workflow can open cases, poll case state, and submit human-task decisions while the existing FastAPI demo remains usable.

## UiPath Components Used

- UiPath Maestro Case: primary case orchestration and governance layer.
- UiPath Automation Cloud: intended runtime environment for the Maestro Case workflow.
- Maestro human tasks: Gate 1 plan review and Gate 2 exception review.
- Maestro service tasks/API calls: call the FraudCase AI external coded agent service.
- External coded agent service: FastAPI/Python service under `fraudcase_ai`, orchestrated by Maestro Case.

Agent type: external coded agent service orchestrated by UiPath Maestro Case.

## How We Use UiPath

Implemented in this repository:

- FraudCase AI exposes a UiPath-facing API contract for opening, reading, and deciding audit cases.
- Every case event includes Maestro-oriented metadata: `case_id`, `maestro_stage`, and `actor_type`.
- Human approval gates are enforced before investigation continues and before findings are written to the audit log.
- Recoverable exceptions are surfaced as case exceptions, then routed to human review before final approval.

Expected UiPath Automation Cloud setup for the final submission:

- UiPath Maestro owns the fraud audit case lifecycle and displays the case state.
- UiPath API Workflows or HTTP activities call the FraudCase AI FastAPI service.
- UiPath human tasks implement Gate 1 plan review and Gate 2 exception review.
- UiPath logs and case history show the orchestration trace, retries, human handoffs, and final report state.
- FraudCase AI remains an external coded agent service; UiPath Maestro and API Workflows orchestrate it.

The helper files in `uipath/` provide the integration contract and case blueprint:

- `uipath/fraudcase-ai-openapi.yaml`
- `uipath/maestro-case-blueprint.md`

## Case Architecture

FraudCase AI tracks case progress through these Maestro stages:

- `case_intake`
- `audit_plan_review`
- `agent_investigation`
- `exception_review`
- `audit_log_write`
- `audit_report_ready`
- `closed`

Server-Sent Events include:

- `case_id`
- `maestro_stage`
- `actor_type`
- event data for plans, tool calls, findings, approvals, writes, and reports

Core API:

- `POST /api/audit-case`
- `GET /api/events/{case_id}`
- `POST /api/approve/{case_id}`
- `GET /api/report/{case_id}`
- `GET /api/status`

Maestro adapter API:

- `POST /api/maestro/audit-cases`
- `GET /api/maestro/audit-cases/{case_id}`
- `POST /api/maestro/audit-cases/{case_id}/decisions`

## Local Setup

The default `.env` is mock-safe and does not require cloud credentials.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn fraudcase_ai.server.app:app --port 8080
```

Open:

```text
http://127.0.0.1:8080
```

Demo flow:

1. Open an audit case from the Audit Case Control panel.
2. Review and approve Gate 1.
3. Watch the agent investigation stream.
4. Review case exceptions in Gate 2.
5. Approve selected findings.
6. Confirm the audit log write and generated report.

Exception-path demo:

Use an objective containing `timeout`, `vendor not found`, `exception`, `low confidence`, or `unclear duplicate`, for example:

```text
Find off-hours payments above $10,000 and simulate a Context Grounding timeout exception.
```

The timeline will show a recoverable case exception before Gate 2 review, demonstrating failure handling and human escalation.

## Demo Video Script

Recommended 3-5 minute flow:

1. Show UiPath Maestro Case or API Workflow screen that opens a FraudCase AI audit case.
2. Start the case with: `Find off-hours payments above $10,000`.
3. Show the generated audit plan.
4. Approve Gate 1 as the human auditor.
5. Show tool calls for UiPath Data Service reads, UiPath Context Grounding evidence retrieval, spend aggregation, policy checks, duplicate detection, ghost vendors, and off-hours payments.
6. Show suspicious invoices and approve/reject Gate 2 findings.
7. Show audit-log write and report generation.
8. Export Markdown, PDF, or Excel.
9. Show a second exception-path case with `timeout` in the objective and the case exception routed to human review.
10. End on the UiPath orchestration/log screen plus the FraudCase AI report.

## Live Mode

Mock mode is enabled by default:

```env
USE_MOCKS=true
```

For live integrations, set `USE_MOCKS=false` and configure the UiPath Automation Cloud,
Data Service, and Context Grounding values in `.env` (see `.env.example`). UiPath Data
Service is the system of record and UiPath Context Grounding owns embeddings, vector
indexing, and retrieval — this service never calls a database or embedding model directly.
With the UiPath endpoints unset, live mode falls back to the local demo dataset so it still
runs credential-free. Never commit real secrets.

## UiPath Coded Agent

The same audit logic ships as a UiPath **coded agent** (UiPath Python SDK) for
deployment to Automation Cloud and orchestration by a Maestro Case. It exposes three
entrypoints — `plan`, `investigate`, `finalize` — in
[`fraudcase_ai/coded_agent/`](fraudcase_ai/coded_agent/), mapped in
[`uipath.json`](uipath.json):

```bash
pip install -r requirements.txt   # includes the `uipath` SDK + CLI
uipath auth && uipath init        # authenticate + discover entrypoints
uipath run investigate '{"objective": "Audit this month vendor payments"}'
uipath pack && uipath publish     # package + deploy to Orchestrator
```

See [docs/agenthack-alignment.md](docs/agenthack-alignment.md) for the full AgentHack
alignment plan (coded agent, Agent Builder reasoning, Maestro Case orchestration).

## Verification

```bash
.venv/bin/python -m pytest -q
python -m compileall fraudcase_ai tests
python -c "from fraudcase_ai.server.app import app; print(app.title)"
```

Expected app title:

```text
FraudCase AI
```

## License

FraudCase AI is licensed under the Apache License, Version 2.0. See `LICENSE` and `NOTICE`.
