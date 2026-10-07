# Automation example: order pricing and audit API

This page preserves a retired end-to-end example and its historical acceptance.
The first Automation card now uses `chat.examples.automation.mixedExecution.prompt`
for the simpler sync/human-approval Task and Deployment acceptance scenario.
The order-audit example below is no longer offered by that card. The
current engine has 16 node types; this example predates Human approval and covers
15 types. Its measurements are not a current capacity guarantee. The historical example asked the Agent to build, test, and publish a Workflow
from one complete instruction.

## Prerequisites

- An available Chat model. Acceptance runs use `codex:account:gpt-6-sol` with
  reasoning effort `high`.
- An enabled manually configured model API for PromptNode and SubAgentNode.
  These nodes discover its authorized name with `config get --scope model_api`;
  a Codex account connection alone does not configure their model.
- Permission to create Workflows and API Deployments. For an unattended run,
  select the permission mode that allows these operations.
- A reachable Flowork public URL. The card uses the current browser origin for
  its HTTP health check, so it follows the deployed domain.

The canonical instruction lives in `chat.examples.automation.orderAudit.prompt`
in the English and Chinese locale files. The acceptance harness reads that exact
text. It sends one user message, never adds hints, and never repairs the graph.
The Agent may diagnose and correct its own work within that turn.

## What it builds

| Nodes | Purpose |
| --- | --- |
| StartNode, EndNode | Typed batch inputs and stable API results |
| CodeNode, TransformNode | Validation, duplicate handling, deterministic prices and transformations |
| ConditionNode | Valid/invalid branches and automatic risk routing |
| LoopBeginNode, LoopEndNode | Price accepted orders individually |
| ParallelStartNode, ParallelEndNode | Run independent health and CSV audit branches, then join |
| HTTPRequestNode | Call the deployment's `/healthz` |
| TableWriteNode, TableReadNode | Write and read execution-local CSV under `/run` |
| SubAgentNode | Inspect that CSV with tools and audit its contents |
| PromptNode | Generate a Chinese summary |
| TemplateNode | Assemble a Markdown report |

All 15 types used by this example must be connected and perform useful work. Model calls
must not calculate authoritative amounts. Empty batches skip model nodes.
The resulting API pins the tested saved version, enables a QPS cap of 1, and
uses `mount=false`. It does not require Chat workspace files to run.

Deliverables in the Chat workspace:

- `automation-order-audit/manifest.json`: resource IDs, fixed version, endpoint,
  credential **path**, node types and test summaries.
- `automation-order-audit/README.md`: contract and invocation instructions.
- `.secrets/order-audit.json`: private credential JSON, mode `0600`. Its
  `api_key` field is the Bearer token; do not preview or commit this file.

## Independent API verification

Download the private credential from your own Chat workspace. Invoke the
existing API without a Flowork login:

```bash
python3 scripts/verify_order_audit_api.py \
  --endpoint 'https://YOUR_FLOWORK/api/v1/deployments/YOUR_SLUG/invoke' \
  --credential-file /private/path/order-audit.json \
  --workflow-json /private/path/saved-graph.json \
  --report /private/path/external-report.json
```

`--workflow-json` is optional; supplying the saved graph also checks all 15 node
types, reachability, and runtime outputs, including outputs inside loops.
LoopEnd does not emit its own output; completion is represented by LoopBegin's
`loop_output`. The graph check does not by itself prove model tool use; inspect
node/task execution evidence when auditing SubAgent behavior.

The script checks that missing credentials return HTTP 401, then sends three
requests sequentially. The request body is the StartNode input object directly,
for example `{"batch_id":"example","orders":[]}`. Do **not** wrap it in
`{"inputs": ...}`. Final values are in `response.outputs`.

| Case | Accepted / duplicate / rejected | Subtotal | Discount | Total | Route |
| --- | --- | --- | --- | --- | --- |
| A: 2×120; B: 1×50 | 2 / 0 / 0 | 290 | 0 | 290 | standard |
| A: 2×120; B: 3×200; C: 1×160; duplicate A; invalid D | 3 / 1 / 1 | 1000 | 10% | 900 | review |
| Empty | 0 / 0 / 0 | 0 | 0 | 0 | invalid |

`review` is an automatic risk label, not a human approval wait. Positive cases
must include successful health and audit checks plus nonempty summary/report.
Empty input must return an explanation and `audit_pass=false`.

## Reproduce the entire one-instruction run

Use the API Python environment (which includes `requests`). Set
`FLOWORK_TEST_EMAIL` and `FLOWORK_TEST_PASSWORD` privately in the process
environment, then run:

```bash
.venv/bin/python scripts/run_automation_example.py \
  --base-url https://YOUR_FLOWORK \
  --language zh \
  --evidence-dir /private/path/NEW-evidence-directory
```

This creates a fresh Project and Chat, submits the canonical instruction once,
records events, downloads the Agent's manifest and exact saved version, and
runs the external verifier. The evidence directory must be new; it contains
private events and the API credential and must not be published. Runs consume
model calls and create persistent resources. A failed run is retained for
diagnosis; the script does not silently retry or coach the Agent.

## Defects found during acceptance

1. **Whole-path interpolation:** the table schema rejected `{{csv_path}}` even
   though its documentation advertised interpolation. Read/write nodes now
   accept an upstream full-path variable. Dynamic values are substituted first,
   then checked against `/run` and `/mount` before file I/O. Missing/non-string
   paths, unresolved placeholders, traversal and symlink escapes are rejected.
2. **External request body ambiguity:** deployment CLI help, returned credential
   hints and Agent guidance now specify the raw StartNode object, the JSON
   credential's `api_key`, and the `outputs` result shape.

A passing run establishes this scenario on the tested configuration. It does
not guarantee that every model or arbitrary Workflow will succeed unattended.

## Recorded acceptance

On 2026-09-29 (Asia/Shanghai), a fresh Chat using sol/high completed this exact
example from one message in about 6½ minutes. Its saved graph contained 22
connected nodes covering all 15 types. TableReadNode used `{{csv_path}}` directly.
The independent external caller passed all three cases and rejected an
unauthenticated request. Private run evidence remains outside version control.
