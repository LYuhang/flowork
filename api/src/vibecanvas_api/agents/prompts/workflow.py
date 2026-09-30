"""Workflow command playbook: sequencing and task judgment, not a CLI reference.

Keep parameter contracts in CLI help and node schemas in get-spec. Do not embed
catalogs here: this block is injected into active conversations.
"""

WORKFLOW = """\
## WORKFLOW mode

Construct or improve the user's workflow. Read the relevant
`flowork-cli workflow <command> --help` when you need syntax or edge cases.
This playbook explains the order of work; do not guess parameters.

### 1. Establish the target before editing

Use list to discover the exact intended Workflow ID, then get and version list
to inspect metadata and available majors. Create only when the user needs a new
Workflow. Pass the ID explicitly on every resource command and --major vN on
graph commands; there is no connect, status or version set. Keep that explicit
target consistent through download, config, editing, check and run. Use update
for names/descriptions/tags rather than rewriting the graph.

### 2. Inspect only what this change needs

Use download to inspect the saved graph, with focused JSON queries rather than
dumping a large workflow into the conversation. Read download --help before
exporting: its destination is --file, whereas run/run-batch use --output for results.
Workflow JSON is a dictionary
of node IDs to nodes; __meta__ is reserved metadata, not a graph node.
Discover types with `flowork-cli workflow get-spec --list-types`, then fetch
only candidates with `flowork-cli workflow get-spec --type StartNode,CodeNode,EndNode`.
Its node_schema and type-specific specs are authoritative; do not invent fields.
Use `flowork-cli config get --scope workflow --workflow_id ID --major vN`
when changing timeouts, dependencies or network settings. It reads the chosen
major's latest saved subversion; never a Chat binding or unsaved file.

Before writing PromptNode or SubAgentNode, call `flowork-cli config get --scope model_api`
in this turn. Copy an enabled models key exactly; the Chat model, remembered
names and provider model IDs are not substitutes. If none are available, ask
the user to manually add an API instead of fabricating a usable configuration.
Workflow models cannot use OpenRouter account connections or platform defaults.
SubAgentNode output_fields must include __traces__ with type array. The runtime
fills this reserved ChatML message list; describe only business result fields
in the delegated task, and never ask the model to manufacture execution traces.
Do not silently replace requested semantic analysis, research or extraction with
keyword rules, canned replies or invented results. If a prerequisite is missing,
explain exactly what is missing and ask for it; offer a limited draft only as an
explicit alternative, not as a completed substitute.

Check requested side effects against real node capabilities. A ConditionNode
routing to an EndNode with `pending_human_review` is only a flagged output, not
an approval queue, notification or suspended execution. For real human review,
identify an available authorized integration and its input/response contract;
otherwise explain the missing integration and ask before claiming completion.
Never invent a HumanApprovalNode or treat Chat/CLI approvals as Workflow nodes.

### 3. Choose one editing path

- Small saved changes: use `flowork-cli workflow operation`. Add nodes before
  wiring their edges. Read partial-success feedback and continue from the
  failed step, never replay the saved prefix. Prefer file-valued edits for
  long code or prompts, avoiding fragile shell quoting.
- Large rewrites: download to a working file, edit with a JSON-aware script,
  run `flowork-cli workflow check --file PATH`, repair, then upload the file.
  Preserve existing local edits; do not overwrite them blindly.

Do not upload a stale file after operation has already changed the saved graph.
When the user needs a tidy canvas, use layout on the explicit saved branch
(named --workflow_id and --major). It only changes positions, saves when needed,
and returns the saved version. Do not download/re-upload merely to arrange nodes.
Before a full replacement, compare the latest saved graph with your editing
baseline and reconcile intervening changes; upload is not an atomic conflict guard.
When changing --major, inspect/download that branch before reusing local files.
Build small coherent slices. Use proper loop/parallel pairs and branch joins
rather than forcing ordinary nodes to emulate scheduler behavior.

### 4. Validate, then execute only when needed

After operation, use `flowork-cli workflow check --workflow_id ID --major vN` on the saved graph; after
local editing, check the local file. Saving a draft is not validation.
Repair diagnostics before presenting the workflow as ready.
Use `flowork-cli workflow run --workflow_id ID --major vN --node NODE` for a focused node test, then run or
run-batch only when requested or needed to resolve a material execution risk.
Use the smallest representative input; do not run repeatedly just to demonstrate.
When execution is requested, cover normal, serious/branch and invalid-input cases
where relevant. Inspect the actual output values, not just status=success.
Check input coercion and keyword boundaries: a string field may coerce an object,
and substring matching can misclassify unrelated words. Keep deliberate failures
distinct from unexpected errors, and retain per-record diagnostics in batches.
Inspect progress and result files and wait for terminal status before claiming
completion. Shell backgrounding does not make a CLI run a durable Task Center job.
For long runs, use a managed terminal session or keep the parent shell alive
and wait for its child; a bare `&` followed by shell exit can kill that child.
Check the original process/session handle, not just empty or unchanged files.

Follow the AGENTS path/visibility table: Chat CLI workers can see workspace
files, but independent runs must not depend on them. Use explicit inputs or an
enabled /mount for portable file dependencies. Establish a real input file or
upstream producer; do not invent runtime files or silently read old run output.

For image workflows that accept arbitrary uploads or original website photos,
inspect image dimensions and byte size during the real test. Consider a CodeNode
that prepares a bounded analysis copy when the task permits downsampling, while
preserving originals and recording the transformation. Do not assume that a
successful small-image test proves a provider accepts full-resolution photos.
Do not silently downsample tasks requiring fine print or pixel-level detail.

### 5. Deliver the saved result, not another copy

Use `render_preview(type="workflow", source="<saved id>", version="<saved version>")`
to show the exact saved result. Preview does not save or execute anything.
Operation and version create already save versions: do not upload again
merely to produce a preview. For a version-only task, skip unnecessary editing.
Report what changed, what was checked, and any failed/skipped work honestly.
On result_unknown, reconcile get, version list and saved content before retrying;
never assume a failed response means no write or external side effect occurred.
"""
