"""Small, stdlib-only CLI, also installed as a standalone sandbox launcher."""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import socket
import stat
import sys
import tempfile
from uuid import uuid4

try:
    from . import task_cli, deployment_cli, knowledge_cli, document_cli, diagram_cli, browser_cli, resource_cli, skill_cli
except ImportError:  # Standalone launcher with the adjacent stdlib-only module.
    import task_cli
    import deployment_cli
    import knowledge_cli
    import document_cli
    import diagram_cli
    import browser_cli
    import resource_cli
    import skill_cli


READ_OPERATIONS = frozenset({"workflow.prepare", "config.get", "workflow.list", "workflow.get", "workflow.download", "workflow.check", "workflow.version.list", "workflow.get-spec"}) | task_cli.READ_OPERATIONS | deployment_cli.READ_OPERATIONS
READ_OPERATIONS = READ_OPERATIONS | knowledge_cli.READ_OPERATIONS | document_cli.OPERATIONS | diagram_cli.OPERATIONS
READ_OPERATIONS = READ_OPERATIONS | browser_cli.READ_OPERATIONS | resource_cli.OPERATIONS | skill_cli.READ_OPERATIONS
WRITE_OPERATIONS = frozenset({"workflow.create", "workflow.update", "workflow.upload", "workflow.operation", "workflow.layout", "workflow.version.create"})
WRITE_OPERATIONS = WRITE_OPERATIONS | {"workflow.delete"} | task_cli.WRITE_OPERATIONS | deployment_cli.WRITE_OPERATIONS
WRITE_OPERATIONS = WRITE_OPERATIONS | knowledge_cli.WRITE_OPERATIONS
WRITE_OPERATIONS = WRITE_OPERATIONS | browser_cli.WRITE_OPERATIONS | skill_cli.WRITE_OPERATIONS
CALL_CONTROLS = frozenset({"cli.start", "cli.poll", "cli.cancel"})
OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS | CALL_CONTROLS


class CliUsageError(ValueError):
    pass


class WorkflowInputError(CliUsageError):
    def __init__(self, code: str, message: str, hint: str):
        super().__init__(message)
        self.code = code
        self.hint = hint


class Parser(argparse.ArgumentParser):
    def parse_known_args(self, args=None, namespace=None):
        # Validate options before argparse mistakes an unknown option's value
        # for the legacy positional workflow ID and reports a target conflict.
        values = list(sys.argv[1:] if args is None else args)
        if any(isinstance(action, WorkflowTarget) for action in self._actions):
            for value in values:
                if value == "--":
                    break
                option = self._parse_optional(value)
                if option is not None and option[0] is None:
                    hint = f"Run {self.prog} --help."
                    if self.prog.endswith("workflow download") and value.split("=", 1)[0] == "--output":
                        hint = "Use --file PATH to save downloaded workflow JSON; omit --file for stdout. --output belongs to run/run-batch."
                    raise WorkflowInputError("invalid_arguments", f"Unrecognized option: {option[1].split('=', 1)[0]}", hint)
        return super().parse_known_args(values, namespace)

    def error(self, message: str) -> None:
        raise CliUsageError(message)


class WorkflowTarget(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            raise CliUsageError("Specify the workflow target only once; supply one explicit workflow ID.")
        setattr(namespace, self.dest, values)


class OrderedOperation(argparse.Action):
    """One shared sequence across all repeatable operation flags."""
    def __call__(self, parser, namespace, values, option_string=None):
        operations = list(getattr(namespace, self.dest, None) or [])
        operations.append((option_string[2:].replace("-", "_"), values))
        setattr(namespace, self.dest, operations)


WORKFLOW_FILE_HELP = (
    'Workflow JSON is a top-level object mapping node IDs to node objects, '
    'for example {"node_1":{"node_id":"node_1","node_type":"StartNode",'
    '"node_config":{},"children":["node_2"]},"node_2":{...}}. '
    'This is a shape illustration, not a complete executable graph. '
    'Do not wrap it in {"nodes":[...]} or add a separate edges array. '
    'Each node lists its outgoing node IDs in children; __meta__ is optional reserved metadata. '
    'get-spec describes individual node/config fields, not a graph envelope. '
    'For small existing-workflow edits prefer workflow operation; for full replacements, download --workflow-id ID --major vN --file PATH, edit that object, '
    'check --file PATH, then upload to the explicit ID and major.'
)


def parser() -> Parser:
    root = Parser(prog="flowork-cli",
        description="Access Flowork resources from an active cloud Agent turn. No login required.",
        epilog="Workflow commands are stateless: pass an exact workflow ID and --major vN for branch content. No connect/disconnect/status/version set. Read each command's --help. Default stdout is one final JSON; progress JSONL goes to stderr. workflow run/run-batch, document/diagram render accept --stream for stdout JSONL; task logs --follow also streams. Events are progress/result/error. command_status is the command outcome, not execution_status or resource_status. Help is text. Workflow download without --file emits the raw workflow JSON only. Exit: 0 success, 1 failure/partial/unknown, 2 invalid input. Never automatically retry a mutation after result_unknown. Transport heartbeat deadlines are not command duration limits.")
    groups = root.add_subparsers(dest="resource", required=True)
    task_cli.add_parser(groups)
    deployment_cli.add_parser(groups)
    knowledge_cli.add_parser(groups)
    document_cli.add_parser(groups)
    diagram_cli.add_parser(groups)
    browser_cli.add_parser(groups)
    resource_cli.add_parser(groups)
    configuration = groups.add_parser("config", help="Read Workflow settings or eligible manually added model APIs.")
    config_actions = configuration.add_subparsers(dest="action", required=True)
    getting = config_actions.add_parser("get", help="Read one configuration scope without secrets.",
        description="--scope workflow requires --workflow-id ID and --major vN. Reads that major's latest saved subversion, never Chat state or unsaved drafts. Output {id,version,settings,defaults}; settings includes timeouts.workflow/code/http, code_requirements, egress.allowed_hosts. defaults.timeouts are seconds, not final per-node limits. --scope model_api takes neither ID nor major and returns {models:{name:{provider,description,context_window_tokens}}}. Only your enabled manually added APIs with live use permission; no OpenRouter account connection, platform default, built-in fallback or secrets. Manual OpenRouter APIs are eligible. Use a models key as node_config.model_name. Empty models is valid; discovery does not test connectivity.")
    getting.epilog = "An empty model catalog includes message and hint explaining the missing prerequisite. Follow the hint; do not invent model names or silently replace requested model analysis with rules."
    getting.add_argument("--scope", required=True, choices=("workflow", "model_api"))
    getting.add_argument("--workflow-id", action=WorkflowTarget, help="Required only for workflow scope.")
    getting.add_argument("--major", help="Required only for workflow scope, e.g. v2.")
    workflow = groups.add_parser("workflow", help="Discover, edit, validate, version, execute and delete workflows.",
        description="Stateless commands. Every resource operation names its target explicitly; branch-content operations also require --major vN. No stored current workflow or selected branch. A command resolves the latest subversion once; run-batch freezes it for all rows. Commits preserve version history and follow the platform's global HEAD save semantics, but never write Chat selection state. Local file metadata never selects the target. Preview is a separate render_preview MCP call.")
    actions = workflow.add_subparsers(dest="action", required=True)

    layout = actions.add_parser("layout", help="Arrange saved nodes left-to-right on an explicit branch.",
        description="Changes only node x/y positions on the specified major's latest saved subversion. Preserves edges, configs and other visual attributes. Saves one new subversion only if positions change. Output {id,version,changed,moved_nodes,message}. Does not validate or execute the graph. No local file or Chat binding. After result_unknown inspect version list/download before retrying.",
        epilog="Example: flowork-cli workflow layout --workflow-id wf_123 --major v2. Use render_preview with the returned id/version to display the saved result; do not upload again.")
    layout.add_argument("--workflow-id", action=WorkflowTarget, required=True, help="Exact workflow ID (named option, not a positional argument).")
    layout.add_argument("--major", required=True, help="Existing major, e.g. v2; arranges its latest saved subversion.")

    def target(command, branch=False, optional=False):
        targets = command.add_mutually_exclusive_group(required=not optional)
        targets.add_argument("--workflow-id", action=WorkflowTarget, help="Exact workflow ID; never inferred from Chat state.")
        targets.add_argument("workflow_id", nargs="?", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        if branch or optional:
            command.add_argument("--major", required=branch and not optional, help="Existing major version, e.g. v2; reads/writes its latest subversion.")

    listing = actions.add_parser("list", help="List workflows you can view, including shared resources.",
        description="Read-only. Output {workflows:[{id,name,description,version}],next_offset}. version is global HEAD. No current_workflow_id or connection. Concurrent changes can affect offset pagination.")
    listing.add_argument("--limit", type=int, default=20, help="Page size, 1–100.")
    listing.add_argument("--offset", type=int, default=0, help="Non-negative offset.")
    creating = actions.add_parser("create", help="Create a workflow; return its ID and v1.sv0, without binding.",
        description="--name required. Optional --file imports a regular UTF-8 JSON object after validation; no fixed byte-size ceiling. Rejects duplicate keys/non-finite numbers. Without a file creates an empty draft. Source identity is not copied. Output {id,name,description,tags,version}; no connected field. Does not execute. After an unknown result inspect list before retrying.")
    creating.add_argument("--name", required=True)
    creating.add_argument("--description", default="")
    creating.add_argument("--tag", action="append", default=[], help="Comma-separated tags; repeatable, trimmed/deduplicated; empty elements rejected.")
    creating.add_argument("--file", help="Optional workflow JSON file.")
    getting_wf = actions.add_parser("get", help="Read Workflow metadata, not a connection.",
        description="Workflow metadata output {id,name,description,tags,version}. version is global HEAD; use version list to discover branches. No graph execution or state change.")
    target(getting_wf)
    updating = actions.add_parser("update", help="Update metadata of the specified workflow.",
        description="Changes only supplied name/description/tags, not graph versions. --description '' clears description; --clear-tags clears tags. --tag accepts comma-separated/repeated values. Output {id,name,description,tags,version}; version is global HEAD. Requires update permission.")
    target(updating)
    updating.add_argument("--name")
    updating.add_argument("--description")
    tags = updating.add_mutually_exclusive_group()
    tags.add_argument("--tag", action="append")
    tags.add_argument("--clear-tags", action="store_true")
    deleting = actions.add_parser("delete", help="Delete an explicitly identified workflow after platform approval.",
        description="Deletes the entire workflow, not one branch. Requires delete permission. agent/always_ask require user approval; always_allow reports automatic approval. No --force/--yes. Active executions or enabled deployments/schedules block deletion. All versions become unavailable; Workflow data/run files are cleaned durably; full recovery is not available. Chat files, memory and mounts remain. Pending approval survives page refresh only while this CLI command lives. Ending the command cancels pending approval, not committed deletion. stderr progress: awaiting_approval, approved/auto_approved; stdout is one final JSON with {id,deleted,cleanup,message}. cleanup:pending means deletion committed, not a reason to retry. No Chat binding is changed.")
    target(deleting)
    downloading = actions.add_parser("download", help="Output the specified branch's workflow JSON, or save it to a file.",
        description="Requires ID and --major. Omit --file to output only the workflow dictionary with __meta__ stamped to the actual saved version. With --file returns {id,version,node_count,path}. Atomic file write; existing files require --overwrite. Never reads/writes Chat state or edits saved versions.")
    target(downloading, branch=True)
    downloading.add_argument("--file", help="Optional destination, relative or absolute sandbox path. Download uses --file, not the run/run-batch --output option.")
    downloading.add_argument("--overwrite", action="store_true")
    uploading = actions.add_parser("upload", help="Replace the specified branch and save a new subversion.",
        description="Requires ID, --major, --file and --expected-version from download. Validates/tidies the graph; preserves history and Workflow metadata. Old file version selectors do not change the target; a different workflow_id is rejected. No force bypass or file rewrite. Output {id,version,node_count}, plus warnings when present. Full replacement atomically rejects a changed target branch tip with version_conflict. Preserve local edits, download and merge; never replace the expected version alone. After result_unknown inspect version list/download, never blindly retry.")
    target(uploading, branch=True)
    uploading.add_argument("--expected-version", required=True, help="Exact downloaded version, e.g. v1.sv3. Conflicts save nothing; download and merge before retrying.")
    uploading.add_argument("--file", required=True)
    uploading.add_argument("--note", default="")
    editing = actions.add_parser("operation", help="Apply ordered atomic edits to an explicitly specified branch.",
        description="Repeat/interleave operation flags in order. Parse all values/files before dispatch. All operations must succeed to save ONE new subversion. Stop at the first business error: nothing is saved and no version is created. stdout is one post-commit report {id,version,applied,total,skipped,results}, plus error/message/hint/failed_index (zero-based)/failed_operation on failure. applied counts saved edits; earlier steps have status not_saved on failure. Exit 1 on failure. Fix the failed operation and resubmit the entire group. Draft edits may be incomplete; check afterward. Requires update permission.")
    target(editing, branch=True)
    for flag, count, metavar, help_text in (
        ("node_add", 1, "JSON", "Add a node object with node_id and node_type. Duplicate IDs are errors."),
        ("node_add_file", 1, "PATH", "Read a UTF-8 JSON node object from a sandbox file."),
        ("node_remove", 1, "NODE_ID", "Remove a node and its incident children edges. Missing node is an error."),
        ("node_update", 2, ("JSON_PATH", "JSON_VALUE"), "Replace a node field using a JSON Pointer and strict JSON value. Strings need JSON double quotes; null is a value, not deletion. Parent objects must exist; arrays are replaced whole. Cannot edit node_id/node_type/children or workflow metadata."),
        ("node_update_file", 2, ("JSON_PATH", "PATH"), "Read the value from a regular sandbox file: plain PATH means UTF-8 text with exact newlines; json:PATH parses any JSON value. Relative paths allowed. Use ./json:name for a literal text filename beginning with json:."),
        ("edge_add", 2, ("SOURCE", "TARGET"), "Append a directed children edge. Endpoints must exist; duplicate edges are errors."),
        ("edge_remove", 2, ("SOURCE", "TARGET"), "Remove a directed children edge. Missing edges are errors."),
    ):
        editing.add_argument("--" + flag.replace("_", "-"), dest="edits", action=OrderedOperation,
                             nargs=count, metavar=metavar, help=help_text)
    editing.epilog = "Example: flowork-cli workflow operation --workflow-id wf_123 --major v1 --node-add-file /data/node.json --node-update-file /node_2/node_config/process_fn /data/process.py --edge-add node_1 node_2. JSON values: --node-update /node_2/node_description '\"Text\"'; --node-update /node_2/node_config/options '{\"limit\":10}'. null is a value, not deletion. Escape '/' in path keys as ~1 and '~' as ~0. New node IDs must match node_[0-9]+ and be unique; node_id/node_type are required. For small edits prefer operation over whole-graph upload; saving an incomplete draft does not establish graph validity. node_config/children default to {}/[]. For multi-node additions use children: [] on each new node, then --edge-add after every endpoint exists. Children must already exist; duplicate IDs/edges and removing missing targets are errors. Node removal clears children edges, not config references. Parent objects must exist; a final field may be new. All files are read in the sandbox; no fixed byte-size ceiling. No new version if any operation fails. Graph/version pointer commit together; only a successful group refreshes the canvas. No graph execution or automatic Preview. After result_unknown inspect version list/download before submitting any further edits."
    editing.add_argument("--note", default="", help="Optional note for the one saved subversion.")

    checking = actions.add_parser("check", help="Check a saved branch OR a local workflow JSON file.",
        description="Exactly one mode: check --workflow-id ID --major vN, or check --file PATH. ID and file cannot be combined; file mode rejects --major. Validate static graph structure, node configuration/references and currently eligible model APIs. No execution, saving or connection. Output {valid,id,version,node_count} for saved content or {valid,path,node_count} for a file, plus errors/warnings. Exit 0 valid, 1 invalid/service error, 2 usage error. Validity does not guarantee execution success.")
    target(checking, optional=True)
    checking.add_argument("--file")
    versions = actions.add_parser("version", help="List branches or create a branch from an explicit source.")
    version_actions = versions.add_subparsers(dest="version_action", required=True)
    version_list = version_actions.add_parser("list", help="List each major's latest subversion.",
        description="Output {id,version,versions:[{major,version}]}; top-level version is global HEAD, not a selected branch. No state changes.")
    target(version_list)
    version_create = version_actions.add_parser("create", help="Copy the specified major's latest snapshot to a new major.",
        description="Requires ID, --major SOURCE and update permission. New major=max+1, subversion=0. Source snapshot is fixed under a row lock; global HEAD advances per platform semantics. No Chat selection, validation or execution. Output {id,previous_version,version}. After result_unknown inspect version list before retrying.")
    target(version_create, branch=True)
    version_create.add_argument("--note", default="")
    spec = actions.add_parser("get-spec", help="Read exact node definitions or list available types.",
        description="No workflow ID required. Output {node_schema,specs} or {types}. Read affected types before constructing or modifying nodes. Combine node_schema with each spec's constraints, config_schema, config_guide and examples; do not infer the contract from examples alone. Unknown/case-mismatched types reject the query. Model eligibility comes from config get --scope model_api, not the schema.")
    for file_command in (creating, uploading, checking, downloading, spec):
        file_command.epilog = WORKFLOW_FILE_HELP
    selection = spec.add_mutually_exclusive_group(required=True)
    selection.add_argument("--type", dest="node_types", action="append", help="Comma-separated exact node types; repeatable.")
    selection.add_argument("--list-types", action="store_true")
    for command in ("run", "run-batch"):
        running = actions.add_parser(command, help="Execute the explicit branch; use --stream for stdout JSONL progress.",
            description="Requires workflow ID, --major and execute permission. Freeze one saved snapshot for the entire invocation/batch; run --file optionally overrides its content without saving. No Chat selection. Default stdout is one final JSON; progress goes to stderr. --stream emits progress and a terminal result/error as stdout JSONL. Business results are flushed to a file. Workflow/node configured execution limits apply; transport deadlines do not cap total execution. The CLI executes locally inside the sandbox. Use setsid nohup and shell output redirection for background execution across Agent turns. SIGINT/SIGTERM cancel this command; sandbox loss ends it. This is not a durable Task. Exit 0 success, 1 failure/partial/unknown, 2 invalid input.")
        target(running, branch=True)
        running.add_argument("--stream", action="store_true", help="Stream JSONL progress and one terminal result/error on stdout; default progress goes to stderr.")
        running.add_argument("--output", help="JSONL result file; default unique /data/runs/<run_id>/results.jsonl, including single-row runs.")
        running.add_argument("--overwrite", action="store_true")
        running.epilog = "Each command owns a local engine runtime. Cancellation stops its active executions and new rows without stopping the Chat sandbox. Results record input/output/node_outputs/errors/execution_time; batch rows append in completion order with zero-based index. Partial files cannot undo external side effects. Check terminal status, not file existence. Never automatically rerun result_unknown. Follow AGENTS.md path visibility rules."
        running.epilog += " HumanApprovalNode waits for review while retaining its worker. Progress includes row_status=waiting_approval and an execution_url for the reviewer; keep this command running in the foreground or with setsid nohup. The original Agent turn need not stay open. Approval progress includes the execution detail link. Rejection produces approved=false and continues. Approval timeout produces no business output and stops that execution with execution_status=timed_out and approval_timeout."
        if command == "run":
            running.add_argument("--node", help="Execute only this exact node ID, not its upstream/downstream nodes. Inputs go directly to the node (overriding configured defaults); no previous outputs are reused. Supports --file. Validates only the target and its required resources. No autosave/new version. Loop/parallel control nodes require full workflow execution. Not supported by run-batch.")
            inputs = running.add_mutually_exclusive_group()
            inputs.add_argument("--input-file", help="UTF-8 JSON object containing workflow inputs.")
            inputs.add_argument("--input", dest="inputs", help="Inline JSON object; default: {}.")
            running.add_argument("--file", help="Optional local workflow JSON override; requires the explicit workflow ID/major and execute permission; does not save.")
        else:
            running.epilog += " Example: flowork-cli workflow run-batch --workflow-id wf_123 --major v1 --input-file /data/rows.csv --output /data/results.jsonl > /data/progress.jsonl 2>&1. For background execution, prefix the command with setsid nohup, redirect output to a log, append < /dev/null &, and retain $! as the PID. Read the local status_path and events_path from progress; require a terminal status and exit_code before claiming completion. Empty output is not evidence of a live process or of no side effects: check the original process/session handle; do not automatically retry. No --file or --node override. Ordinary row errors continue; authorization/transport/platform errors stop new rows."
            running.add_argument("--input-file", required=True, help="CSV/TSV/JSON/JSONL/XLSX/XLSM table. JSON: array of objects or {rows: [...]}; JSONL: one object per line.")
            running.add_argument("--concurrency", type=int, default=4, help="Positive requested worker count (default: 4; at most 16 active workers).")
            running.add_argument("--sheet", default="", help="Workbook sheet; required when multiple sheets exist.")
            running.add_argument("--name", help="Batch name; defaults to input filename.")

    return root


def emit_progress(value: dict, *, stream: bool = False) -> None:
    """Progress never contaminates a normal command's machine-readable result."""
    if not isinstance(value, dict):
        raise ValueError("Invalid CLI progress frame.")
    print(json.dumps({**value, "event": "progress"}, ensure_ascii=False),
          file=sys.stdout if stream else sys.stderr, flush=True)


def emit_result(value: dict, *, exit_code: int = 0, state_field: str | None = None) -> int:
    """Keep business fields compatible while separating command outcome."""
    result = dict(value)
    if state_field and "status" in result:
        result.setdefault(state_field, result["status"])
    result["command_status"] = ("unknown" if result.get("error") == "result_unknown"
                                else "failed" if exit_code else "succeeded")
    result["event"] = "error" if exit_code else "result"
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return exit_code


def error(code: str, message: str, hint: str) -> dict:
    return {"error": code, "message": message, "hint": hint}


def parse_tags(values: list[str]) -> list[str]:
    """Normalize both repeated flags and comma-separated tags; also used by Host."""
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise CliUsageError("tags must be a list of strings.")
    tags = [tag.strip() for value in values for tag in value.split(",")]
    if any(not tag for tag in tags):
        raise CliUsageError("Tags cannot contain empty elements. Omit --tag, or use update --clear-tags to remove all tags.")
    return list(dict.fromkeys(tags))


def validate_arguments(operation: str, arguments: dict) -> dict:
    """Shared syntax validation; runtime/Host repeat it for socket requests."""
    if operation in skill_cli.OPERATIONS:
        return skill_cli.validate(operation, arguments)
    if operation in resource_cli.OPERATIONS:
        return resource_cli.validate(operation, arguments)
    if operation.startswith("browser."):
        try:
            return browser_cli.validate(operation, arguments)
        except ValueError as exc:
            raise CliUsageError(str(exc)) from exc
    if operation.startswith("diagram."):
        try:
            return diagram_cli.validate(operation, arguments)
        except ValueError as exc:
            raise CliUsageError(str(exc)) from exc
    if operation.startswith("document."):
        try:
            return document_cli.validate(operation, arguments)
        except ValueError as exc:
            raise CliUsageError(str(exc)) from exc
    if operation.startswith("knowledge."):
        try:
            return knowledge_cli.validate(operation, arguments)
        except ValueError as exc:
            raise CliUsageError(str(exc)) from exc
    if operation.startswith(("task.", "deployment.")):
        try:
            return (task_cli if operation.startswith("task.") else deployment_cli).validate(operation, arguments)
        except ValueError as exc:
            raise CliUsageError(str(exc)) from exc
    allowed = {
        "cli.start": {"call_id", "operation", "arguments"},
        "cli.poll": {"call_id", "ack"},
        "cli.cancel": {"call_id"},
        "workflow.delete": {"workflow_id"},
        "config.get": {"scope", "workflow_id", "major"},
        "workflow.list": {"limit", "offset"},
        "workflow.create": {"name", "description", "tags", "workflow"},
        "workflow.update": {"workflow_id", "name", "description", "tags"},
        "workflow.get": {"workflow_id"},
        "workflow.download": {"workflow_id", "major"},
        "workflow.prepare": {"workflow_id", "major", "workflow", "node", "run_id"},
        "workflow.upload": {"workflow_id", "major", "workflow", "note", "expected_version"},
        "workflow.layout": {"workflow_id", "major"},
        "workflow.operation": {"workflow_id", "major", "operations", "note"},
        "workflow.check": {"workflow_id", "major", "workflow"},
        "workflow.get-spec": {"node_types", "list_types"},
        "workflow.version.list": {"workflow_id"},
        "workflow.version.create": {"workflow_id", "major", "note"},
    }
    if operation not in allowed or set(arguments) - allowed[operation]:
        raise CliUsageError("Unsupported operation or arguments.")
    result = dict(arguments)
    if operation in CALL_CONTROLS:
        if not isinstance(result.get("call_id"), str) or re.fullmatch(r"[a-f0-9]{32}", result["call_id"]) is None:
            raise CliUsageError("Invalid CLI call ID.")
        if operation == "cli.start":
            target = result.get("operation")
            if not isinstance(target, str) or target not in (READ_OPERATIONS | WRITE_OPERATIONS):
                raise CliUsageError("Unsupported CLI call operation.")
            if not isinstance(result.get("arguments"), dict):
                raise CliUsageError("Invalid CLI call arguments.")
            result["arguments"] = validate_arguments(target, result["arguments"])
        if operation == "cli.poll" and (type(result.get("ack")) is not int or result["ack"] < 0):
            raise CliUsageError("Invalid CLI acknowledgement.")
    branch_ops = {"workflow.prepare", "workflow.download", "workflow.upload", "workflow.operation", "workflow.layout", "workflow.version.create"}
    target_ops = branch_ops | {"workflow.update", "workflow.get", "workflow.delete", "workflow.version.list"}
    if operation == "config.get":
        if result.get("scope") not in ("workflow", "model_api"):
            raise CliUsageError("scope must be workflow or model_api.")
        if result["scope"] == "model_api" and ({"workflow_id", "major"} & result.keys()):
            raise CliUsageError("--workflow-id and --major are only supported with --scope workflow.")
        if result["scope"] == "workflow":
            branch_ops.add(operation)
            target_ops.add(operation)
    if operation == "workflow.check":
        if "workflow" in result:
            if "workflow_id" in result or "major" in result:
                raise CliUsageError("Use either a workflow ID with --major, or --file, not both.")
        else:
            branch_ops.add(operation)
            target_ops.add(operation)
    if operation in target_ops:
        value = result.get("workflow_id")
        if not isinstance(value, str) or not value.strip():
            raise CliUsageError("An explicit workflow ID is required.")
        result["workflow_id"] = value.strip()
    if operation in branch_ops:
        major = result.get("major")
        if not isinstance(major, str) or re.fullmatch(r"v[1-9][0-9]*", major) is None:
            raise CliUsageError("--major must specify an existing major such as v2.")
    if operation == "workflow.operation":
        result.setdefault("note", "")
        if not isinstance(result["note"], str):
            raise CliUsageError("note must be a string.")
        edits = result.get("operations")
        if not isinstance(edits, list) or not edits:
            raise CliUsageError("Supply at least one operation flag.")
        fields = {"node_add": {"op", "node"}, "node_remove": {"op", "node_id"},
                  "node_update": {"op", "path", "value"},
                  "edge_add": {"op", "source", "target"}, "edge_remove": {"op", "source", "target"}}
        for edit in edits:
            if not isinstance(edit, dict) or not isinstance(edit.get("op"), str) or edit["op"] not in fields or set(edit) != fields[edit["op"]]:
                raise CliUsageError("Invalid operation envelope or unsupported fields.")
            # Domain checks belong to ordered execution, not this preflight.
            for key in ("node_id", "path", "source", "target"):
                if key in edit and not isinstance(edit[key], str):
                    raise CliUsageError(f"Operation {key} must be a string.")
    if operation == "workflow.get-spec":
        if set(result) == {"list_types"} and result["list_types"] is True:
            pass
        elif set(result) == {"node_types"}:
            values = result["node_types"]
            if not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values):
                raise CliUsageError("--type must specify one or more node type names.")
            types = [name.strip() for value in values for name in value.split(",")]
            if any(not name for name in types):
                raise CliUsageError("--type cannot contain empty elements.")
            result["node_types"] = list(dict.fromkeys(types))
        else:
            raise CliUsageError("Specify either --type or --list-types, not both.")
    if operation == "workflow.prepare":
        if not isinstance(result.get("run_id"), str) or re.fullmatch(r"[0-9a-f]{32}", result["run_id"]) is None:
            raise CliUsageError("run_id must be a UUID hex string.")
        if "workflow" in result and not isinstance(result["workflow"], dict):
            raise CliUsageError("Workflow must be a JSON object.")
        if "node" in result and (not isinstance(result["node"], str) or not result["node"].strip() or result["node"].startswith("__")):
            raise CliUsageError("--node must be an exact nonblank node ID, not a reserved metadata key.")
    if operation == "workflow.version.create":
        result.setdefault("note", "")
        if not isinstance(result["note"], str):
            raise CliUsageError("note must be a string.")
    if operation == "workflow.list":
        result.setdefault("limit", 20)
        result.setdefault("offset", 0)
        if type(result["limit"]) is not int or not 1 <= result["limit"] <= 100:
            raise CliUsageError("--limit must be between 1 and 100")
        if type(result["offset"]) is not int or not 0 <= result["offset"] <= 2_147_483_647:
            raise CliUsageError("--offset must be between 0 and 2147483647")
    elif operation == "workflow.create":
        key = "name" if operation == "workflow.create" else "workflow_id"
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise CliUsageError(f"{key} must be a nonblank string.")
        result[key] = result[key].strip()
        if operation == "workflow.create":
            result.setdefault("description", "")
            result.setdefault("tags", [])
            if not isinstance(result["description"], str):
                raise CliUsageError("description must be a string.")
            result["tags"] = parse_tags(result["tags"])
            if "workflow" in result and not isinstance(result["workflow"], dict):
                raise CliUsageError("The workflow file must contain a JSON object.")
    elif operation == "workflow.update":
        if not (set(result) - {"workflow_id"}):
            raise CliUsageError("Supply at least one of --name, --description, --tag, or --clear-tags.")
        if "name" in result:
            if not isinstance(result["name"], str) or not result["name"].strip():
                raise CliUsageError("name must be a nonblank string.")
            result["name"] = result["name"].strip()
        if "description" in result and not isinstance(result["description"], str):
            raise CliUsageError("description must be a string.")
        if "tags" in result:
            result["tags"] = parse_tags(result["tags"])
    elif operation in {"workflow.upload", "workflow.check"}:
        if (operation == "workflow.upload" or "workflow" in result) and not isinstance(result.get("workflow"), dict):
            raise CliUsageError("--file must contain a workflow JSON object.")
        if operation == "workflow.upload":
            if not isinstance(result.get("expected_version"), str) or not re.fullmatch(r"v[1-9]\d*\.sv\d+", result["expected_version"]):
                raise CliUsageError("--expected-version must be an exact downloaded version such as v1.sv3.")
            result.setdefault("note", "")
            if not isinstance(result["note"], str):
                raise CliUsageError("note must be a string.")
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CliUsageError("Arguments must be finite, valid UTF-8 JSON.") from exc
    return result


def strict_json(value: str):
    """Parse any JSON value without duplicate keys, NaN or Infinity."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    result = json.loads(value, object_pairs_hook=unique_object, parse_constant=reject_constant)
    json.dumps(result, allow_nan=False).encode("utf-8")
    return result


def operation_arguments(edits, note: str) -> dict:
    operations = []
    try:
        for flag, values in edits or []:
            if flag == "node_add":
                edit = {"op": "node_add", "node": strict_json(values[0])}
            elif flag == "node_add_file":
                edit = {"op": "node_add", "node": strict_json(read_operation_file(values[0]).lstrip("\ufeff"))}
            elif flag in {"node_update", "node_update_file"}:
                path, value = values
                if flag == "node_update":
                    value = strict_json(value)
                elif value.startswith("json:"):
                    value = strict_json(read_operation_file(value[5:]).lstrip("\ufeff"))
                else:
                    value = read_operation_file(value)
                edit = {"op": "node_update", "path": path, "value": value}
            elif flag == "node_remove":
                edit = {"op": flag, "node_id": values[0]}
            else:
                edit = {"op": flag, "source": values[0], "target": values[1]}
            operations.append(edit)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, WorkflowInputError):
            raise
        raise CliUsageError(f"Invalid operation JSON: {exc}") from exc
    return {"operations": operations, "note": note}


def read_operation_file(path: str) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Operation input must be a regular file.")
            return stream.read().decode("utf-8")
    except FileNotFoundError as exc:
        raise WorkflowInputError("file_not_found", "The operation input file does not exist.", "Check the sandbox file path before retrying. No edits were submitted.") from exc
    except OSError as exc:
        raise WorkflowInputError("file_read_failed", "The operation input file could not be read.", "Check the sandbox file path and read permissions. No edits were submitted.") from exc
    except ValueError as exc:
        raise WorkflowInputError("invalid_input_file", "Operation input must be a regular UTF-8 file.", "Repair the input file before retrying. No edits were submitted.") from exc


def read_workflow(path: str) -> dict:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("--file must point to a regular file")
            data = stream.read()
        value = strict_json(data.decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("--file must contain a workflow JSON object")
        return value
    except FileNotFoundError as exc:
        raise WorkflowInputError("file_not_found", "The workflow file does not exist.", "Check --file or run workflow download first.") from exc
    except OSError as exc:
        raise WorkflowInputError("file_read_failed", "The workflow file could not be read.", "Check --file, file type, and read permissions.") from exc
    except (ValueError, RecursionError) as exc:
        raise WorkflowInputError("invalid_workflow_file", f"Cannot read workflow JSON: {exc}", "Use a regular UTF-8 JSON object file without duplicate keys or non-finite numbers.") from exc


def uncertain_result() -> dict:
    return error("result_unknown", "The command ended without a confirmed result; changes may have committed.",
                 "Do not automatically repeat create, delete, update, upload, operation, version create, run, or run-batch. Use flowork-cli workflow get --workflow-id ID, version list and download to reconcile the explicitly targeted branch; use list to find a possibly created workflow. Inspect partial results and external side effects before retrying.")


def save_download(result: dict, path: str, *, overwrite: bool) -> dict:
    """Write beside the destination, then publish atomically (no clobber by default)."""
    destination = os.path.abspath(path)
    temporary = None
    try:
        workflow = result.get("workflow")
        if not isinstance(workflow, dict):
            raise ValueError("The platform returned no workflow object.")
        data = (json.dumps(workflow, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=".flowork-download-", dir=os.path.dirname(destination))
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)
        return {key: result[key] for key in ("id", "version", "node_count")} | {"path": destination}
    except FileExistsError:
        return error("file_exists", "The destination already exists; it was not replaced.", "Choose another --file path or pass --overwrite.")
    except (OSError, ValueError, KeyError, RecursionError) as exc:
        return error("download_failed", str(exc), "Check destination permissions, parent directory, and available disk space before retrying.")
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass  # Do not replace the business result with a cleanup error.


def request(socket_path: str, arguments: dict, *, operation: str = "workflow.list", on_progress=None) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(60)
        connection.connect(socket_path)
        body = {"operation": operation, "arguments": arguments}
        connection.sendall(json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")
        with connection.makefile("rb") as stream:
            while True:
                response = stream.readline()
                if not response.endswith(b"\n"):
                    raise ValueError("incomplete CLI response")
                frame = json.loads(response)
                if isinstance(frame, dict) and frame.get("_transport") == "heartbeat":
                    continue  # Idle deadline, never a total business deadline.
                if isinstance(frame, dict) and "_progress" in frame:
                    if on_progress is not None:
                        on_progress(frame["_progress"])
                    else:
                        emit_progress(frame["_progress"])
                    continue
                break
        if not response.endswith(b"\n"):
            raise ValueError("incomplete CLI response")
        result = json.loads(response)
        if operation in task_cli.OPERATIONS | deployment_cli.OPERATIONS | knowledge_cli.OPERATIONS | document_cli.OPERATIONS | diagram_cli.OPERATIONS | browser_cli.OPERATIONS | resource_cli.OPERATIONS | skill_cli.OPERATIONS and isinstance(result, dict):
            return result
        if not isinstance(result, dict) or not ("workflows" in result or "error" in result or "connected" in result or (operation == "workflow.delete" and result.get("deleted") is True) or ("id" in result and "version" in result) or (operation == "config.get" and arguments.get("scope") == "model_api" and isinstance(result.get("models"), dict)) or (operation == "workflow.check" and type(result.get("valid")) is bool) or (operation == "workflow.get-spec" and isinstance(result.get("types" if arguments.get("list_types") else "specs"), list))):
            raise ValueError("invalid CLI response")
        return result


def _read_run_file(path: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CliUsageError("Input must be a regular file.")
            return stream.read()
    except OSError as exc:
        raise CliUsageError(f"Could not read input file: {path}.") from exc


def _run_json(text: str) -> dict:
    def reject(value):
        raise ValueError(f"Invalid JSON number: {value}")
    try:
        value = json.loads(text, parse_constant=reject)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (ValueError, UnicodeError) as exc:
        raise CliUsageError("Inputs must contain a finite JSON object.") from exc


def _open_run_output(path: str, *, overwrite: bool, sources: list[str]):
    for source in sources:
        if os.path.realpath(source) == os.path.realpath(path) or (
            os.path.exists(path) and os.path.samefile(source, path)
        ):
            raise CliUsageError("Output must not replace an input or workflow file.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW
    if not overwrite:
        flags |= os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        target = os.fstat(fd)
        if not stat.S_ISREG(target.st_mode):
            raise CliUsageError("Output must be a regular file, not a symlink or device.")
        for stream in (sys.stdout, sys.stderr):
            try:
                redirected = os.fstat(stream.fileno())
            except (OSError, ValueError, AttributeError):
                continue
            if (target.st_dev, target.st_ino) == (redirected.st_dev, redirected.st_ino):
                raise CliUsageError("Result output and redirected stdout/stderr must use different files.")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        return os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def execute_command(args, endpoint: str) -> int:
    from vibecanvas_api.flowork_cli.local_command import execute
    return execute(args, endpoint, sys.modules[__name__])


def main(argv: list[str] | None = None, *, socket_path: str | None = None) -> int:
    args = None
    try:
        args = parser().parse_args(argv)
        if args.resource == "skill" and args.action in skill_cli.ACTIONS:
            return skill_cli.execute(args, socket_path or os.environ.get("FLOWORK_CLI_SOCKET", ""), sys.modules[__name__])
        if args.resource in {"skill", "mcp"}:
            return resource_cli.execute(args, socket_path or os.environ.get("FLOWORK_CLI_SOCKET", ""), sys.modules[__name__])
        if args.resource == "browser":
            return browser_cli.execute(args, socket_path or os.environ.get("FLOWORK_CLI_SOCKET", ""), sys.modules[__name__])
        if args.resource == "diagram":
            return diagram_cli.execute(args, socket_path or os.environ.get("FLOWORK_CLI_SOCKET", ""), sys.modules[__name__])
        if args.resource == "document":
            return document_cli.execute(args, socket_path or os.environ.get("FLOWORK_CLI_SOCKET", ""), sys.modules[__name__])
        if args.resource == "knowledge":
            return knowledge_cli.execute(args, socket_path or os.environ.get("FLOWORK_CLI_SOCKET", ""), sys.modules[__name__])
        if args.resource == "task":
            return task_cli.execute(args, socket_path or os.environ.get("FLOWORK_CLI_SOCKET", ""), sys.modules[__name__])
        if args.resource == "deployment":
            return deployment_cli.execute(args, socket_path or os.environ.get("FLOWORK_CLI_SOCKET", ""), sys.modules[__name__])
        if args.action in {"run", "run-batch"}:
            return execute_command(args, socket_path or os.environ.get("FLOWORK_CLI_SOCKET", ""))
        if args.action == "download" and args.overwrite and args.file is None:
            raise CliUsageError("--overwrite requires --file.")
        operation = f"{args.resource}.{args.action}"
        if args.resource == "config":
            arguments = {"scope": args.scope}
            if args.workflow_id is not None:
                arguments["workflow_id"] = args.workflow_id
            if args.major is not None:
                arguments["major"] = args.major
        elif args.action == "version":
            operation = f"workflow.version.{args.version_action}"
            arguments = {"workflow_id": args.workflow_id}
            if args.version_action == "create":
                arguments.update(note=args.note, major=args.major)
        elif args.action == "list":
            arguments = {"limit": args.limit, "offset": args.offset}
        elif args.action == "get-spec":
            arguments = {"list_types": True} if args.list_types else {"node_types": args.node_types}
        elif args.action == "operation":
            arguments = {**operation_arguments(args.edits, args.note), "workflow_id": args.workflow_id, "major": args.major}
        elif args.action in {"get", "delete"}:
            arguments = {"workflow_id": args.workflow_id}
        elif args.action in {"download", "layout"}:
            arguments = {"workflow_id": args.workflow_id, "major": args.major}
        elif args.action == "upload":
            arguments = {"workflow_id": args.workflow_id, "major": args.major, "workflow": read_workflow(args.file), "note": args.note, "expected_version": args.expected_version}
        elif args.action == "check":
            arguments = {"workflow": read_workflow(args.file)} if args.file is not None else {}
            if args.workflow_id is not None:
                arguments["workflow_id"] = args.workflow_id
            if args.major is not None:
                arguments["major"] = args.major
        elif args.action == "update":
            arguments = {key: getattr(args, key) for key in ("name", "description") if getattr(args, key) is not None}
            arguments["workflow_id"] = args.workflow_id
            if args.clear_tags:
                arguments["tags"] = []
            elif args.tag is not None:
                arguments["tags"] = args.tag
        else:
            arguments = {"name": args.name, "description": args.description, "tags": args.tag}
        arguments = validate_arguments(operation, arguments)
        if args.action == "create" and args.file is not None:
            arguments["workflow"] = read_workflow(args.file)
            arguments = validate_arguments(operation, arguments)
    except WorkflowInputError as exc:
        # Retain earlier create/upload error contracts; check exposes specific
        # local input codes so the Agent can distinguish I/O from graph errors.
        if args is None or args.action in {"check", "operation"}:
            emit_result(error(exc.code, str(exc), exc.hint), exit_code=2)
        else:
            emit_result(error("invalid_arguments", str(exc), "Run flowork-cli <resource> <command> --help."), exit_code=2)
        return 2
    except CliUsageError as exc:
        emit_result(error("invalid_arguments", str(exc), "Run flowork-cli <resource> <command> --help."), exit_code=2)
        return 2
    endpoint = socket_path or os.environ.get("FLOWORK_CLI_SOCKET", "")
    if not endpoint:
        result = error(
            "runtime_unavailable", "No active Flowork Agent execution is connected.",
            "Run this command inside an active Flowork cloud Agent turn.",
        )
    else:
        try:
            result = request(endpoint, arguments, operation=operation)
        except TimeoutError:
            result = uncertain_result() if operation in WRITE_OPERATIONS else error("request_timeout", "The read-only query timed out.", "This read-only query can be retried.")
        except OSError:
            result = uncertain_result() if operation in WRITE_OPERATIONS else error(
                "runtime_unavailable", "The Agent execution connection is no longer available.",
                "Run the command again from a new active Agent turn; do not reuse an old launcher.",
            )
        except (ValueError, UnicodeError):
            result = uncertain_result() if operation in WRITE_OPERATIONS else error("invalid_response", "The platform returned an invalid response.", "Retry or contact platform support.")
        except KeyboardInterrupt:
            result = uncertain_result() if operation in WRITE_OPERATIONS else error("cancelled", "The command was interrupted.", "No workflow was changed.")
    if args.action == "download" and "error" not in result:
        if args.file is not None:
            result = save_download(result, args.file, overwrite=args.overwrite)
        elif isinstance(result.get("workflow"), dict):
            # Business node IDs may be named error/valid: do not interpret graph
            # keys as the transport error envelope or a check result.
            print(json.dumps(result["workflow"], ensure_ascii=False))
            return 0
        else:
            result = error("invalid_response", "The platform returned no workflow object.", "Retry or contact platform support.")
    if args.action == "check" and args.file is not None and "error" not in result:
        result = {**result, "path": os.path.abspath(args.file)}
    return emit_result(result, exit_code=1 if "error" in result or result.get("valid") is False else 0)


if __name__ == "__main__":
    sys.exit(main())
