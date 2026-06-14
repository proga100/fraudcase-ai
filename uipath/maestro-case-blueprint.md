# UiPath Maestro Case Blueprint

This is the intended UiPath Automation Cloud workflow for the AgentHack Track 1 submission. FraudCase AI is the external coded agent service. UiPath Maestro Case owns orchestration, governance, human tasks, and case auditability.

## Case Stages

1. `case_intake`
2. `audit_plan_review`
3. `agent_investigation`
4. `exception_review`
5. `audit_log_write`
6. `audit_report_ready`
7. `closed`

## UiPath Components

- Maestro Case: case lifecycle, state, human task routing, auditability.
- API Workflow or HTTP activities: calls to FraudCase AI adapter endpoints.
- Human task 1: Gate 1 plan review.
- Human task 2: Gate 2 finding and exception review.
- External coded agent service: FraudCase AI FastAPI service that performs planning, investigation, exception handling, and report generation.

## Workflow

### 1. Case Intake

Trigger: auditor submits an objective, for example:

```text
Find off-hours payments above $10,000 and duplicate vendor payments.
```

UiPath API call:

```http
POST /api/maestro/audit-cases
Content-Type: application/json

{
  "text": "Find off-hours payments above $10,000 and duplicate vendor payments."
}
```

Persist returned `case_id` as a Maestro case field.

### 2. Audit Plan Review

FraudCase AI emits a plan and waits at Gate 1.

UiPath human task:

- Show generated plan.
- Actions: approve, edit and approve, reject.

Decision callback:

```http
POST /api/maestro/audit-cases/{case_id}/decisions
Content-Type: application/json

{
  "gate": "plan",
  "approved": true,
  "edited_plan": null
}
```

### 3. Agent Investigation

FraudCase AI executes audit tools:

- MongoDB vector search for semantically similar fraud patterns.
- MongoDB aggregation for spend/risk grouping.
- Duplicate invoice detection.
- Policy-threshold checks.
- Ghost-vendor detection.
- Off-hours payment checks.
- OFAC/sanctions screening.

UiPath should poll:

```http
GET /api/maestro/audit-cases/{case_id}
```

Use `maestro_stage` to update the case view.

### 4. Exception Review

Suspicious findings and recoverable tool failures are case exceptions.

Example exception-path objective:

```text
Find off-hours payments above $10,000 and simulate a MongoDB timeout exception.
```

UiPath human task:

- Show flagged invoices.
- Show recoverable exception messages.
- Approve or reject each finding.

Decision callback:

```http
POST /api/maestro/audit-cases/{case_id}/decisions
Content-Type: application/json

{
  "gate": "action",
  "approved": true,
  "approved_ids": ["INV-001", "INV-002"],
  "rejected_ids": ["INV-003"]
}
```

### 5. Audit Log Write

Only approved findings are written to the audit log. This is the controlled business action.

UiPath should show this as a service-task completion after Gate 2.

### 6. Report Ready

Fetch report:

```http
GET /api/report/{case_id}
```

The web UI can export Markdown, PDF, and Excel artifacts for the demo.

### 7. Close Case

When `maestro_stage` is `closed` and `report_ready` is `true`, close the Maestro case or route it to remediation.

## Failure And Retry Handling

Recommended UiPath policy:

- If FraudCase AI returns a recoverable `exception` event, route to the Exception Review human task.
- If an API call times out, retry 2 times with exponential backoff.
- If retries fail, mark the Maestro case as `Needs manual investigation`.
- Never write to audit log without a Gate 2 decision.

## Demo Evidence To Show

- Maestro case instance with `case_id`.
- Gate 1 human task.
- Tool-call timeline.
- Exception review path.
- Gate 2 human task.
- Audit-log write.
- Report export.
- UiPath job/log history proving the platform orchestrated the workflow.
