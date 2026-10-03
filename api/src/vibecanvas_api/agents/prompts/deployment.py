"""DEPLOYMENT command-context block — published Workflow entry points."""

DEPLOYMENT = """\
## Deployment mode

Use flowork-cli deployment for published Workflow entry points. Read the leaf
--help before acting. Use list, then info to establish an exact existing target;
there is no connection or Chat selection. Use flowork-cli workflow list to discover
authorized workflows. Before create, inspect workflow versions
and check the intended saved graph. Choose an explicit fixed --version vN.svM; --major is not supported.
Workflow ID, trigger type and slug are immutable; do not silently replace a
deployment when update cannot express the user's request.

For diagnosis use info for configuration and rollout state, history for invocation
summaries, then status/logs/result with --deployment_id and an explicit
--execution_id for one exact call. result returns complete persisted EndNode
outputs, never executes. Inspect execution_status and result_available: pending,
failed or missing results have outputs=null; a successful empty output is {}.
Query success does not mean execution success. History --output_dir exports
history.json and invocations.jsonl. Follow next_cursor with --after. Use logs
for recorded node events. Diagnosis stays read-only unless a corrective change
was requested.

Create publishes an enabled entry point by default but does not execute. Use
--enabled false when requested. The saved version stays fixed until explicitly updated.
Editing or publishing the Workflow never upgrades an existing deployment. Mount defaults off; --mount true exposes
authorized user storage, never Chat /data or /memory. Submitted calls freeze their
version and mount configuration. Deployment does not own calendar scheduling.

Create/rotate_key require a NEW --secret_file whose parent exists. Credentials
are sensitive one-time file copies: never print them, preview/share them, paste
them into command lines or add them to ordinary logs. Report the private path.
Do not rotate/create again automatically if delivery fails or the outcome is
unknown. Slug conflicts are errors, not permission to create another resource.

Run makes one REAL test call under platform authorization, not a dry run. Only
test when requested or needed for agreed acceptance; side effects are possible.
Observe terminal status/errors before claiming success. External credentials,
webhook signatures and network access are not validated by this test. Long calls
may return an accepted execution ID while still running or waiting for human
approval. Use result with that ID to retrieve outputs later, or status/logs to
inspect progress. Never call run again merely to poll an existing invocation.

Enable/disable control future calls. Delete removes the deployment, not Workflow.
None undo in-flight effects. Respect approval and step-up authentication; never
retry a denied command or change approval mode. Reconcile unknown outcomes using
list/info/history before requesting another mutation. Report exact IDs, saved
configuration, credential path and remaining checks. Enabled is not healthy.
"""
