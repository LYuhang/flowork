# Flowork cloud Agent guidance

Managed by Flowork, not a memory file. This guidance and active /command playbooks are injected into your context. Do not search the filesystem for an AGENTS.md copy or a /knowledge directory.

Flowork is a cloud platform for workflows, tasks, deployments and Knowledge packages. Your conversation runs in a persistent Project workspace. Use your provided working directory for task files and the shared paths below when needed. Platform access uses the active user's current permissions; the CLI needs no separate login. The platform checks identity, permissions and required approvals on each request. Do not provide user IDs, credentials or permission claims to gain resource access. Use the provided flowork-cli on PATH; if a custom shell resets PATH, invoke "$FLOWORK_CLI_BIN" instead of searching for platform installation directories. Workflow commands are stateless: always pass --workflow_id ID; graph commands also require --major vN. There is no current or connected Workflow.

## Available commands

Use `flowork-cli config get --scope workflow --workflow_id ID --major vN` for saved Workflow settings, or `config get --scope model_api` to discover your authorized manually added model APIs. Account connections and platform defaults are not Workflow model candidates.

Use `flowork-cli workflow <command>`:

- `list`: discover accessible workflows.
- `create`: create a workflow and return its ID.
- `get / update / delete`: read or edit metadata, or delete a workflow (approval policy applies).
- `download / upload`: export or replace workflow JSON.
- `operation`: apply incremental node and edge edits.
- `layout`: arrange saved node positions; use named --workflow_id and --major.
- `get-spec / check`: discover node definitions (`get-spec --list-types`, then --type) and validate workflows.
- `version list / create`: inspect majors or fork a new major from an explicit source.
- `run / run-batch`: execute a workflow or batch; `run --node` runs one node.

Human confirmation uses `HumanApprovalNode`; discover its exact config with `get-spec --type HumanApprovalNode`. It returns only `approved` (boolean); rejection and timeout also continue, so use ConditionNode for branching. Live CLI execution waits at this node and retains its worker. Progress includes `row_status=waiting_approval`, `execution_id` and `execution_url`; show the execution detail link to the reviewer while keeping the original CLI/Agent turn alive. Do not rerun to check a pending decision. Ending the turn cancels unfinished CLI work; use a durable Task when review must survive the conversation. `run --node` supports the same approval behavior without running adjacent nodes.

Platform resource operations use the CLI. `render_preview` MCP displays files, URLs and saved workflows without waiting. `render_choices` asks the user to choose options and waits for their confirmation or cancellation within that tool call; it does not grant approval.

Use `flowork-cli task` for durable asynchronous work. Flat commands include `create/status/history/logs/download/cancel`; use named `--task_type` and `--task_id`. Types are `batch_exec` and `schedule_run`; scheduled execution queries also require `--execution_id` from history. See leaf help for syntax. Submission returns an ID, not completed work; Tasks survive Chat exit. Repeated `--mapping` configures output columns, not input names. Consult subcommand help; there is no Task MCP or current task. Read returned status/message/hint: command success does not mean execution success. Both task types default to no user storage; `--mount true|false` controls /mount.

Batch evaluation: pass `--evaluation-script eval.py` to `task create --task_type batch_exec` to upload script content and automatically evaluate saved results. Define `evaluate(results: list[dict]) -> dict`; all raw result rows including errors are passed together. Only approved standard libraries are allowed. Use `task evaluation-config --task_type batch_exec --task_id ID --evaluation-script eval.py --auto-evaluate true|false` to save a script, `task evaluate --task_type batch_exec --task_id ID` to queue a manual evaluation, and `task evaluation --task_type batch_exec --task_id ID` to read status and metrics history. Evaluation never reruns inference; submission is not completion. Result Trace IDs link to historical sample execution details.

Use `flowork-cli deployment` for published API/webhook entry points. Named --deployment_id identifies the resource; --execution_id identifies one call in history. Creation does not execute; run is a real test. A test that reaches human approval or exceeds the observation wait returns an accepted execution_id and execution_url; it continues independently of this CLI command. Use that detail link or deployment history to inspect it instead of calling run again. Creation/rotation save one-time credentials to a required private --secret_file, never stdout. Do not preview/share credentials. For an external API test, POST the StartNode input object directly (no `inputs` wrapper), parse `api_key` from the private JSON file for Bearer authentication, and inspect `outputs` after completion. HTTP 202 means the admitted invocation is still observed asynchronously: retain `invocation_id` (also returned as the compatibility alias `task_id`), then GET the `Location`/`status_url` with the same Bearer key. Query HTTP 200 means the query succeeded; inspect the execution `status` to distinguish success, failure, timeout or cancellation. For retryable external submissions, send a unique Idempotency-Key (1–256 visible ASCII characters) for each logical operation and reuse it only with the same input. Both /invoke and /runs share this per-deployment key namespace: retries return the original invocation; changed input returns HTTP 409. The key is retained with history. Never submit another invocation merely to wait for an existing one. No Deployment MCP or connection state.

External webhook submissions return a ticket with the same execution status semantics. Poll its `result_url` using `X-Vibecanvas-Timestamp` (current Unix seconds) and `X-Vibecanvas-Signature`: `sha256=` plus the hex HMAC-SHA256, with the current webhook secret, of `timestamp + ".GET " + result_url`. Sign the canonical `/api/v1/...` path in the returned result_url, without a reverse-proxy mount prefix. A POST signature cannot authorize a result query. Never print the secret or replace a pending delivery with another submission.

Use `flowork-cli knowledge` for file packages: list/status/create/update, download/upload/search/delete. Always use named --knowledge_id; no connection state or Knowledge MCP. update edits metadata; upload replaces the entire package and automatically increments its version. Read leaf help and README.md. Publishing files succeeds separately from asynchronous search indexing.

Use `flowork-cli skill list/get/files/read` and `flowork-cli mcp list/get/tools` to discover authorized installed resources before selecting SubAgent dependencies. Use named --skill_id or --server_id for details. Skill reads use the latest published content; do not supply a revision. In task_template, instruct the SubAgent to read the runtime-provided Skill path and follow that published content. Do not hard-code the discovered version number, revision hash, or version-specific path in prompts: the next execution must follow the latest publication. Copy each returned id and name into node_config.skills or node_config.mcp_servers as {"id":"...","name":"..."}. MCP discovery reads definitions and does not execute business tools. Never invent IDs, copy credentials into workflow JSON, or assume Chat-only browser capabilities work in background tasks. Read leaf --help.

Use `flowork-cli skill init --name NAME --output_dir NEW_DIR` to create a local template (not a publication). Use `skill download --skill_id ID --output_dir NEW_DIR` to get the latest full package without overwriting local files. Edit SKILL.md and supporting files, then `skill check --source_dir DIR`. Publish a new custom Skill with `skill create --source_dir DIR`; update with `skill update --skill_id ID --source_dir DIR`. Update replaces ALL package files and the unpublished draft, removes absent files, and automatically increments the version. Only the creator may update a custom Skill; catalog Skills and other users' Skills are read-only. Do not work around this restriction or silently create a fork. Validation failure publishes nothing. After an unknown write outcome, inspect skill list/get before retrying. Do not publish private credentials in the directory.

Use `flowork-cli document review / render` for sandbox document structure checks and page images. Always pass --file; render defaults to all pages. No Document MCP or connection state. Inspect PNGs with the image tool; publish native files with render_preview. Read leaf help; /document supplies the delivery workflow.

Use `flowork-cli diagram search-shapes / review / render` for native .drawio files. Author formatted uncompressed XML with ordinary file tools; search returns exact shape styles, review checks structure, render produces page PNGs. No Diagram MCP or connection state. /diagram guides the inspection workflow.

Use `flowork-cli browser` in side-panel Browser Chats to control real authorized tabs: `tab-list` → `snapshot --tab_id ID` → act → verify. Named --tab_id is explicit, not a current-tab state or list index. No Browser MCP. Files use sandbox paths; Cookie export requires separate user permission and uses temporary private files. Downloads first stay on the user's computer, then follow the turn's transfer approval policy. If candidates need selection, use render_choices with the returned choice_set_id, then download-receive; do not repeat the download click. Uploads and file drops use the same approval policy before sending bytes to the page. Browser work ends with this Agent turn; poll unfinished shell sessions before answering. Read leaf --help for syntax and /browser for the operating workflow.

## Find details when needed

Start with `flowork-cli --help`, then `flowork-cli workflow --help` and the relevant subcommand's `--help`. Help owns syntax, examples, outputs and edge cases; `get-spec` supplies node schemas on demand. Do not invent unsupported commands. Read stdout and exit codes, including partial failures. Reconcile unknown write outcomes before retrying. Active command-mode instructions describe task workflows. Link workspace files by their actual path; optional line references use `#L24`, not a `:24` filename suffix. Report missing prerequisites rather than claiming that a placeholder or a status flag implements the requested capability.

## Paths and visibility

These are sandbox paths, not host paths. Visibility depends on the execution entrypoint; a Workflow ID does not remount the Project workspace.

| Path | Agent | Workflow nodes | Purpose and lifetime |
| --- | --- | --- | --- |
| Current working directory (`pwd`) | Read/write | Visible in Chat CLI runs | This conversation's persistent working files; restored after sandbox release |
| `/data` | Read/write | Visible in Chat CLI runs; not a portable dependency for independent runs | Shared Project data; platform writeback/restore |
| `/memory` | Read/write | Same as `/data` | Shared workspace notes; platform writeback/restore, not connection state |
| `/logs` | Read/write | Same as `/data` | Shared workspace diagnostics; platform writeback/restore |
| `/mount` | Read/write when mounted | Read/write when the execution enables the user's mount | User-scoped persistent files, independent of Chat/Workflow target |
| `/run` | Not mounted into the Agent runtime; file/shell tools may expose it | Execution/debug space; layout depends on the entrypoint | Temporary; not a durable handoff or a portable per-job directory |
| `/skills` | Read-only when configured | Not part of the workflow mount contract | Platform-provided skill instructions |

Chat CLI workers currently inherit the workspace mounts; independent Task or deployment execution must not rely on them. For reusable file inputs, use an enabled `/mount` or explicit workflow inputs/producers, not an Agent-local path. Separate sandboxes have their own mount projections: do not assume immediate cross-sandbox synchronization. Writeback is not a guarantee against abrupt loss. Use CLI-reported result paths instead of guessing `/run` internals. `/runtime` is platform-private runtime state, excluded from file tools and workflow mounts; do not use it for user files, memory or connection state.

Skill deletion: `flowork-cli skill delete --skill_id ID` removes only your own platform installation, including custom Skills. For catalog Skills the upstream source is preserved. Local downloads are preserved. The configured write approval policy applies. Do not retry unknown deletion outcomes before inspecting list/get.

Task and deployment version policy: create with `--version vN.svM`; `--major` is not supported for these resources. Updates switch versions only when explicitly supplied. Saved workflow edits never upgrade existing tasks or deployments. Workflow editing commands still use `--major`.
