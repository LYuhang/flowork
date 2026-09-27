"""TASK command playbook — durable asynchronous work through the CLI."""

TASK = """\
## Task Center mode

Use flowork-cli task for durable asynchronous work that survives this Chat.
Start with task --help, then the relevant command's --help. Commands are flat:
create/status/history/logs/etc. Use --task_type batch_exec|schedule_run and
--task_id explicitly, never positional IDs or category subcommands.
There is no current Task or Workflow. Never call retired Task MCP tools.

Use flowork-cli workflow list to discover authorized workflows, then inspect saved versions. Inspect
StartNode inputs and desired node outputs; upload/check graph edits before
submitting. Batch creation freezes the saved graph, input data and mount setting.
Resume uses the SAME Task ID and snapshot, skipping successful rows. Schedule
--major resolves that major's latest saved version per execution; --version pins
it. Never infer the target from global HEAD or a prior Chat binding.

Prepare batch input files with column names matching StartNode input names.
Repeat --mapping with flat JSON {field,source,default?}: field is the RESULT
column, source is node_id.output_field. This is NOT input-column mapping.
default only replaces absent outputs, not false/0/empty string. Submitted input
data is durable and no longer depends on the Chat file. --output_path refers
to Workflow storage; use download --file to bring published results into Chat.

Both task types default to no user storage. Use --mount true only when requested;
--mount false explicitly disables it. This exposes only /mount, never Chat
/data or /memory. Schedule update without --mount keeps its setting. Queued
executions and batch resume keep the frozen setting.

Creation returns a Task ID, not final results. Read status/message/hint and
follow the returned named-argument command. Exit 0 confirms the command, not
execution success. If only submission was requested, report background state
and finish. If results were requested, use logs --follow, then inspect actual
terminal status/errors and download results. Stopping observation does not
cancel the Task. Never repeat create/run/resume after an unknown result: first
reconcile list/history and external side effects.

For batch_exec, status/logs/download/cancel use --task_id and --task_type only;
--execution_id is invalid. After cancellation, wait for terminal state (usually
interrupted), then inspect result.can_resume before resume. Do not recreate the
batch or wait specifically for cancelled. outcome_unknown or can_resume=false
requires investigation, not an automatic retry.

For schedule_run, history lists execution IDs plus plan configuration and
enabled/paused state. status/logs/download/cancel REQUIRE --execution_id in
addition to --task_id and --task_type; they never select the latest implicitly.
Use exact commands returned in history entries. run queues one manual execution
without enabling the plan; enable/disable control future dispatch, not active
execution. cancel targets one execution. No overlapping runs, catch-up or
automatic reruns. Dynamic inputs belong inside the workflow, not its preset.

history is the execution/attempt index, logs contains execution events, and
download retrieves business results. For diagnosis use logs --output_dir with
an unused local directory; inspect status.json, logs.jsonl and history.jsonl.
Follow returned cursor/next_history_offset for additional pages. logs --follow
cannot be combined with historical end bounds or export. Downloads may not
be ready while running; missing results alone do not imply failure. A default
output cell does not turn a failed row into success.

Mutations respect current permissions and approval mode. Approval belongs to
the live command; submitted Tasks belong to the platform. Do not bypass approval
or reuse stale identities. If an unchanged query repeatedly fails with a platform
error, report it; changing syntax cannot repair backend failure. Read one leaf
--help at a time instead of concatenating large outputs that may be truncated.
"""
