"""DEPLOYMENT command-context block — published Workflow entry points."""

DEPLOYMENT = """\
## Deployment mode

Use flowork-cli deployment for published Workflow entry points. Read the leaf
--help before acting. Use list, then status to establish an exact existing target;
there is no connection or Chat selection. Use flowork-cli workflow list to discover
authorized workflows. Before create, inspect workflow versions
and check the intended saved graph. Choose an explicit fixed --version vN.svM; --major is not supported.
Workflow ID, trigger type and slug are immutable; do not silently replace a
deployment when update cannot express the user's request.

For diagnosis use status, then history with a relevant time window. Use
--execution_id for one exact call and --output_dir for searchable status.json,
metrics.json and invocations.jsonl. Follow next_cursor with --after. These are
invocation summaries, not full node logs. Query success does not mean execution
success. Diagnosis stays read-only unless the user requests a corrective change.

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
have no fixed CLI duration cutoff; use a live terminal session, not a detached
child of an exiting shell. If observation ends, inspect history before another
action: the accepted invocation may still be running.

Enable/disable control future calls. Delete removes the deployment, not Workflow.
None undo in-flight effects. Respect approval and step-up authentication; never
retry a denied command or change approval mode. Reconcile unknown outcomes using
list/status/history before requesting another mutation. Report exact IDs, saved
configuration, credential path and remaining checks. Enabled is not healthy.
"""
