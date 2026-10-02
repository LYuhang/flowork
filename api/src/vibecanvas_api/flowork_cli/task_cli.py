"""Stdlib-only Task command contract, shared by the sandbox and Host.

The launcher ships this module alongside cli.py. No platform credentials,
database clients or backend imports belong in this module.
"""
from __future__ import annotations

import base64
from datetime import datetime
import json
import os
import re
import tempfile
from uuid import UUID


READ_OPERATIONS = frozenset("task." + name for name in (
    "list", "status", "history", "logs", "download", "evaluation",
))
WRITE_OPERATIONS = frozenset("task." + name for name in (
    "evaluate", "evaluation-config", "create", "update", "enable", "disable", "run", "cancel", "resume", "delete",
))
OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS
FIXED_COLUMNS = ("index", "status", "error", "execution_time")


def _command(parent, name, **kwargs):
    # argparse's help= appears only on the parent; leaf --help needs the same
    # behavioral contract, otherwise an Agent sees flags without semantics.
    kwargs.setdefault("description", kwargs.get("help"))
    kwargs.setdefault("allow_abbrev", False)
    return parent.add_parser(name, **kwargs)


def add_parser(groups):
    task = _command(groups, "task", help="Manage durable asynchronous tasks.",
        description="Use fixed subcommands and named arguments only. --task_type is batch_exec or schedule_run. Submission is asynchronous: read status, message and hint; exit 0 confirms the command, not execution success. Tasks survive Chat exit. Run each subcommand's --help for details.")
    actions = task.add_subparsers(dest="action", required=True)

    def typed(command, required=True):
        command.add_argument("--task_type", required=required, choices=("batch_exec", "schedule_run"))

    def target(command, execution=False):
        typed(command)
        command.add_argument("--task_id", required=True, help="Task ID returned by create/list, never an execution ID.")
        if execution:
            command.add_argument("--execution_id", help="Required for schedule_run; forbidden for batch_exec. Discover it with task history.")

    def page(command):
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--offset", type=int, default=0)

    target(_command(actions, "evaluation", help="Batch only: read evaluation configuration, status and metrics history."))
    target(_command(actions, "evaluate", help="Batch only: evaluate saved results asynchronously; never reruns inference. Inspect task evaluation for completion."))
    evaluation_config = _command(actions, "evaluation-config", help="Batch only: save evaluation script content and optional automatic evaluation setting.")
    target(evaluation_config)
    evaluation_config.add_argument("--evaluation-script", dest="evaluation_script", required=True)
    evaluation_config.add_argument("--auto-evaluate", dest="auto_evaluate", choices=("true", "false"), default=None)

    listing = _command(actions, "list", help="List authorized tasks; --task_type optionally filters one type.")
    typed(listing, False)
    page(listing)
    listing.add_argument("--status", help="Comma-separated task statuses.")
    listing.add_argument("--workflow_id")
    listing.add_argument("--query")

    status = _command(actions, "status", help="Read one execution's status, progress and result. schedule_run requires --execution_id; no implicit latest execution.")
    target(status, True)
    history = _command(actions, "history", help="List past scheduled executions or batch attempts, newest first. Also returns task configuration and plan state when applicable.")
    target(history)
    page(history)
    logs = _command(actions, "logs", help="Read execution logs or follow incremental progress.",
        description="schedule_run requires --execution_id; batch_exec rejects it. Without --follow returns one page with logs, cursor and has_more; use --after CURSOR to continue. --follow streams JSONL until the selected execution is terminal, then reports actual status/errors. Stopping observation never cancels execution. --output_dir exports this page plus diagnostic context to a NEW directory; use returned cursors for more pages. Log cursor IDs are not task/execution IDs.")
    target(logs, True)
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--after", type=int, default=0)
    logs.add_argument("--before", type=int, help="Exclusive end cursor, e.g. a batch attempt's historical log boundary.")
    logs.add_argument("--limit", type=int, default=50)
    logs.add_argument("--from", dest="from_time", help="ISO-8601 with timezone.")
    logs.add_argument("--to", dest="to_time", help="ISO-8601 with timezone.")
    logs.add_argument("--output_dir")
    download = _command(actions, "download", help="Download published business results, not logs. schedule_run requires --execution_id.")
    target(download, True)
    download.add_argument("--format", choices=("csv", "jsonl", "xlsx", "json"), help="Batch: csv/jsonl/xlsx, default jsonl. Schedule execution: json only.")
    download.add_argument("--file", required=True)
    download.add_argument("--overwrite", action="store_true")

    for action in ("create", "update"):
        command = _command(actions, action,
            help="Create a batch task or recurring plan." if action == "create" else "Update supplied schedule fields; never changes existing execution snapshots.",
            description=("batch_exec requires --workflow_id, --version and --input_file, then submits asynchronously. Input columns match StartNode names. Repeat --mapping '{\"field\":\"answer\",\"source\":\"node_3.answer\",\"default\":null}' for OUTPUT columns, not input mapping. default only replaces absent outputs, never false/0/empty string. Fixed index/status/error/execution_time columns remain. schedule_run requires --workflow_id, --version and --interval/--cron. Creates an ENABLED schedule by default; --paused only saves configuration. Read the returned hint; do not repeat submission."
                if action == "create" else "schedule_run only. Updates supplied fields; does not enable a paused schedule. --inputs/--inputs_file replaces the complete preset. Use --clear_start_at/--clear_end_at to remove bounds. Use enable/disable for future dispatch."))
        if action == "create":
            typed(command)
            command.add_argument("--workflow_id", required=True)
        else:
            target(command)
        selector = command.add_mutually_exclusive_group(required=action == "create")
        selector.add_argument("--version", help="Pinned saved version, e.g. v1.sv2.")
        command.add_argument("--name", help="schedule_run only: plan display name; defaults to the workflow ID plus 'schedule'. Batch tasks do not have a custom name.")
        timing = command.add_mutually_exclusive_group()
        timing.add_argument("--interval", type=int, help="schedule_run only: positive interval in seconds; create requires interval or cron.")
        timing.add_argument("--cron", help="schedule_run only: five-field cron expression.")
        command.add_argument("--timezone", help="schedule_run only: IANA timezone; defaults to UTC at creation.")
        for boundary in ("start", "end"):
            bounds = command.add_mutually_exclusive_group()
            bounds.add_argument(f"--{boundary}_at", help="schedule_run only: ISO-8601 with timezone.")
            if action == "update":
                bounds.add_argument(f"--clear_{boundary}_at", action="store_true")
        inputs = command.add_mutually_exclusive_group()
        inputs.add_argument("--inputs", help="Schedule input JSON object.")
        inputs.add_argument("--inputs_file", help="Read schedule input JSON from a local file now.")
        command.add_argument("--mount", choices=("true", "false"),
            help="Expose user storage /mount. Creation defaults to false; omission during update preserves current setting. Frozen on submission; batch resume reuses it. Never shares Chat /data or /memory.")
        command.add_argument("--notify", help="Schedule: succeeded,failed or none; defaults to failed.")
        if action == "create":
            command.add_argument("--paused", action="store_true", default=None, help="schedule_run only: create a paused plan; otherwise enabled.")
            command.add_argument("--evaluation-script", dest="evaluation_script", help="Batch only: upload a Python evaluate(results) script and evaluate automatically after inference. No third-party imports.")
            command.add_argument("--input_file", help="Batch: CSV/TSV/JSON/JSONL/XLSX/XLSM.")
            command.add_argument("--input_sheet")
            command.add_argument("--mapping", action="append", help="Batch output: flat JSON {field,source,default?}; repeat for ordered columns.")
            command.add_argument("--concurrency", type=int, help="Batch parallel rows, 1–16; default 1.")
            command.add_argument("--output_path", help="Batch result destination in Workflow storage, NOT Chat /data.")
            command.add_argument("--output_sheet")

    for action, description in {
        "enable": "schedule_run only. Enable future dispatch; no catch-up of missed occurrences.",
        "disable": "schedule_run only. Disable future dispatch; does not cancel an active execution.",
        "run": "schedule_run only. Queue one manual execution; does not enable the schedule. Returns before execution finishes.",
        "cancel": "Request cancellation, not confirmation of completion. schedule_run requires --execution_id. Batch cancellation normally becomes interrupted; check result.can_resume before resuming.",
        "resume": "batch_exec only. Resume the SAME Task ID from a durable checkpoint, preserving its snapshot and skipping successful rows. Requires result.can_resume=true and a terminal state; never repeat while resuming/running. Unknown outcomes require inspection, not automatic reruns.",
        "delete": "Delete a task and its execution history after approval; active execution blocks deletion. Does not delete the Workflow.",
    }.items():
        target(_command(actions, action, help=description), execution=action == "cancel")


def validate(operation, arguments):
    """Validate the same strict contract on both sides of the sandbox socket."""
    if operation not in OPERATIONS or not isinstance(arguments, dict):
        raise ValueError("Unsupported Task operation or arguments.")
    supplied_type = arguments.get("task_type")
    if (not isinstance(supplied_type, str) or supplied_type not in {"batch_exec", "schedule_run"}) and (operation != "task.list" or supplied_type is not None):
        raise ValueError("--task_type is required: batch_exec or schedule_run.")
    if operation in {"task.update", "task.enable", "task.disable", "task.run"} and supplied_type != "schedule_run":
        raise ValueError("This operation only supports --task_type schedule_run.")
    if operation == "task.resume" and supplied_type != "batch_exec":
        raise ValueError("resume only supports batch_exec; use enable for future schedule dispatch.")
    if operation in {"task.evaluation", "task.evaluate", "task.evaluation-config"} and supplied_type != "batch_exec":
        raise ValueError("Evaluation only supports batch_exec.")
    if operation in WRITE_OPERATIONS:
        action = {"task.enable": "resume", "task.disable": "pause"}.get(operation, operation.split(".")[1])
        operation = f"task.{supplied_type}.{action}"
    common = {"task_id"}
    schedule_fields = {"name", "major", "version", "interval", "cron", "timezone",
                       "start_at", "end_at", "inputs", "mount", "notify"}
    allowed = {
        "task.evaluation": common | {"task_type"},
        "task.batch_exec.evaluate": common,
        "task.batch_exec.evaluation-config": common | {"evaluation_script", "auto_evaluate"},
        "task.list": {"task_type", "status", "workflow_id", "query", "limit", "offset"},
        "task.status": common | {"task_type", "execution_id"},
        "task.logs": common | {"task_type", "execution_id", "follow", "after", "before", "limit", "from_time", "to_time", "export"},
        "task.download": common | {"task_type", "execution_id", "format"},
        "task.batch_exec.create": {"workflow_id", "major", "version", "data", "format", "input_sheet", "mapping", "concurrency", "output_path", "output_sheet", "mount", "evaluation_script"},
        "task.schedule_run.create": schedule_fields | {"workflow_id", "paused"},
        "task.schedule_run.update": schedule_fields | common,
        "task.history": common | {"task_type", "limit", "offset"},
        "task.schedule_run.cancel": common | {"execution_id"},
    }
    for name in ("task.batch_exec.cancel", "task.batch_exec.resume", "task.batch_exec.delete",
                 "task.schedule_run.pause", "task.schedule_run.resume", "task.schedule_run.run", "task.schedule_run.delete"):
        allowed[name] = common
    allowed = {key: values | {"task_type"} for key, values in allowed.items()}
    if operation not in allowed:
        raise ValueError("Unsupported Task operation or arguments.")
    unsupported = arguments.keys() - allowed[operation]
    if unsupported:
        flags = ", ".join("--" + key for key in sorted(unsupported))
        raise ValueError(f"Unsupported parameters for {supplied_type or 'task list'}: {flags}. Check this command's --help for type-specific options.")
    result = dict(arguments)
    if "evaluation_script" in result:
        script = result["evaluation_script"]
        if not isinstance(script, str) or not script.strip() or len(script) > 65536:
            raise ValueError("Evaluation script must contain 1–65536 characters.")
    if "auto_evaluate" in result and type(result["auto_evaluate"]) is not bool:
        raise ValueError("auto_evaluate must be a boolean.")
    if operation in READ_OPERATIONS:
        if operation != "task.list" and "task_type" not in result:
            raise ValueError("--task_type is required: batch_exec or schedule_run.")
        if "task_type" in result and result["task_type"] not in {"batch_exec", "schedule_run"}:
            raise ValueError("--task_type must be batch_exec or schedule_run.")
        if operation in {"task.status", "task.logs", "task.download"}:
            if result["task_type"] == "schedule_run" and not result.get("execution_id"):
                raise ValueError("--execution_id is required for schedule_run; find it with task history --task_id ID --task_type schedule_run.")
            if result["task_type"] == "batch_exec" and "execution_id" in result:
                raise ValueError("--execution_id is not valid for batch_exec.")
    if result.get("follow") and any(result.get(key) is not None and result.get(key) is not False for key in ("export", "before", "to_time")):
        raise ValueError("--follow cannot be combined with --output_dir, --before or --to.")
    for key in ("task_id", "execution_id"):
        if key == "task_id" and key in allowed[operation] and key not in result:
            raise ValueError("An explicit Task ID is required.")
        if key in result:
            if not isinstance(result[key], str):
                raise ValueError(f"{key} must be a UUID.")
            try:
                result[key] = str(UUID(result[key]))
            except ValueError as exc:
                raise ValueError(f"{key} must be a UUID returned by task list/status.") from exc
    if operation == "task.schedule_run.cancel" and "execution_id" not in result:
        raise ValueError("--execution_id is required; cancelling an execution does not pause the plan.")
    for key in ("workflow_id", "name", "timezone", "query", "output_path", "output_sheet"):
        if key in result and (not isinstance(result[key], str) or not result[key].strip()):
            raise ValueError(f"{key} must be a nonempty string.")
    for key in ("follow", "mount", "paused", "export"):
        if key in result and type(result[key]) is not bool:
            raise ValueError(f"{key} must be a boolean.")
    for key, low, high in (("limit", 1, 200 if operation == "task.logs" else 100),
                           ("offset", 0, 2147483647),
                           ("after", 0, 9223372036854775807), ("before", 1, 9223372036854775807),
                           ("concurrency", 1, 16), ("interval", 1, 2147483647)):
        if key in result and (type(result[key]) is not int or not low <= result[key] <= high):
            raise ValueError(f"{key} must be between {low} and {high}.")
    for key, pattern in (("major", r"v[1-9][0-9]*"), ("version", r"v[1-9][0-9]*\.sv[0-9]+")):
        if key in result and (not isinstance(result[key], str) or not re.fullmatch(pattern, result[key])):
            raise ValueError(f"Invalid --{key}; use v2 for major or v2.sv3 for version.")
    if "major" in result:
        raise ValueError("Tasks require a fixed --version, e.g. v2.sv3; --major is no longer supported.")
    if operation.endswith(".create"):
        if not result.get("workflow_id") or not result.get("version"):
            raise ValueError("Creation requires --workflow_id and --version.")
    if "inputs" in result and not isinstance(result["inputs"], dict):
        raise ValueError("Inputs must be a JSON object.")
    if "interval" in result and "cron" in result:
        raise ValueError("--interval and --cron are mutually exclusive.")
    if "cron" in result and (not isinstance(result["cron"], str) or len(result["cron"].split()) != 5):
        raise ValueError("--cron must have five fields.")
    if operation == "task.schedule_run.create" and not ({"interval", "cron"} & result.keys()):
        raise ValueError("Supply --interval or --cron.")
    if operation == "task.schedule_run.update" and result.keys() == {"task_id", "task_type"}:
        raise ValueError("Supply at least one schedule field to update.")
    for key in ("start_at", "end_at", "from_time", "to_time"):
        if key in result and result[key] is not None:
            try:
                value = datetime.fromisoformat(result[key].replace("Z", "+00:00"))
                if value.utcoffset() is None:
                    raise ValueError("missing offset")
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(f"{key} must be an ISO-8601 timestamp with timezone.") from exc
    enums = {"status": {"queued", "running", "resuming", "finished", "finished_with_errors", "failed", "interrupted", "cancelling", "cancelled", "enabled", "paused"},
             "notify": {"succeeded", "failed", "none"}}
    for key, choices in enums.items():
        if key in result:
            if not isinstance(result[key], str) or any(x not in choices for x in result[key].split(",")):
                raise ValueError(f"{key} must contain comma-separated values from {', '.join(sorted(choices))}.")
            if key == "notify" and "none" in result[key].split(",") and result[key] != "none":
                raise ValueError("--notify none cannot be combined with notification events.")
    if operation == "task.download":
        formats = {"json"} if supplied_type == "schedule_run" else {"csv", "jsonl", "xlsx"}
        result.setdefault("format", "json" if supplied_type == "schedule_run" else "jsonl")
        if result["format"] not in formats:
            raise ValueError("Download format for this task type must be " + ", ".join(sorted(formats)) + ".")
    if operation == "task.batch_exec.create":
        if not isinstance(result.get("data"), str) or result.get("format") not in {"csv", "tsv", "json", "jsonl", "xlsx", "xlsm"}:
            raise ValueError("A supported input table file is required.")
        if not isinstance(result.get("input_sheet", ""), str):
            raise ValueError("input_sheet must be a string.")
        if result.get("input_sheet") and result["format"] not in {"xlsx", "xlsm"}:
            raise ValueError("--input_sheet only applies to Excel inputs.")
        if result.get("output_sheet") and not str(result.get("output_path", "")).lower().endswith(".xlsx"):
            raise ValueError("--output_sheet requires an .xlsx --output_path.")
        mappings = result.setdefault("mapping", [])
        if not isinstance(mappings, list):
            raise ValueError("mapping must be a list of flat JSON objects.")
        seen = set(FIXED_COLUMNS)
        for index, item in enumerate(mappings, 1):
            if not isinstance(item, dict) or set(item) - {"field", "source", "default"} or not {"field", "source"} <= item.keys():
                raise ValueError(f"Mapping {index}: use {{field,source,default?}}.")
            field, source = item["field"], item["source"]
            if not isinstance(field, str) or not field.strip() or field in seen:
                raise ValueError(f"Mapping {index}: field must be nonempty and unique, including platform metadata columns.")
            if not isinstance(source, str) or "." not in source or not all(source.split(".", 1)):
                raise ValueError(f"Mapping {index}: source must be node_id.output_field.")
            seen.add(field)
    json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return result


def execute(args, endpoint, api):
    """Materialize local files, then make exactly one submission/observation."""
    operation = "task." + args.action
    local_keys = {"resource", "action", "task_action", "input_file", "inputs_file",
                  "file", "overwrite", "output_dir", "clear_start_at", "clear_end_at"}
    arguments = {key: value for key, value in vars(args).items() if key not in local_keys and value is not None}
    output = None
    staged = None
    dispatched = False
    try:
        if getattr(args, "evaluation_script", None):
            if args.task_type != "batch_exec":
                raise ValueError("--evaluation-script only supports batch_exec.")
            arguments["evaluation_script"] = api._read_run_file(args.evaluation_script).decode("utf-8-sig")
        if "auto_evaluate" in arguments:
            arguments["auto_evaluate"] = arguments["auto_evaluate"] == "true"
        if "mount" in arguments:
            arguments["mount"] = arguments["mount"] == "true"
        if operation == "task.logs" and args.output_dir:
            arguments["export"] = True
        if operation == "task.create" and args.task_type == "batch_exec":
            if not args.input_file:
                raise ValueError("--input_file is required for batch_exec creation.")
            if args.inputs_file:
                raise ValueError("--inputs_file is only valid for schedule_run.")
            arguments.update(data=base64.b64encode(api._read_run_file(args.input_file)).decode("ascii"),
                             format=os.path.splitext(args.input_file)[1][1:].lower())
            mappings = []
            for index, raw in enumerate(args.mapping or [], 1):
                try:
                    mappings.append(api.strict_json(raw))
                except ValueError as exc:
                    raise ValueError(f"Mapping {index}: invalid JSON: {exc}") from exc
            arguments["mapping"] = mappings
        if args.task_type == "schedule_run" and args.action in {"create", "update"}:
            if getattr(args, "input_file", None):
                raise ValueError("--input_file is only valid for batch_exec; schedule_run uses --inputs_file.")
            if args.inputs_file is not None:
                arguments["inputs"] = api._run_json(api._read_run_file(args.inputs_file).decode("utf-8-sig"))
            elif args.inputs is not None:
                arguments["inputs"] = api._run_json(args.inputs)
            for boundary in ("start", "end"):
                if getattr(args, f"clear_{boundary}_at", False):
                    arguments[f"{boundary}_at"] = None
        arguments = validate(operation, arguments)
        if not endpoint:
            raise RuntimeError("No active Flowork Agent turn is connected.")
        if operation == "task.download":
            path = os.path.abspath(args.file)
            if not args.overwrite and os.path.lexists(path):
                raise ValueError("Destination exists; use --overwrite explicitly.")
            fd, staged = tempfile.mkstemp(prefix=".flowork-download-", dir=os.path.dirname(path))
            output = os.fdopen(fd, "wb")

        def progress(value):
            if output is not None and "chunk" in value:
                output.write(base64.b64decode(value["chunk"], validate=True))
                output.flush()
            else:
                print(json.dumps(value, ensure_ascii=False), flush=True)

        dispatched = True
        result = api.request(endpoint, arguments, operation=operation, on_progress=progress)
        if "error" not in result and operation == "task.download":
            output.close()
            if args.overwrite:
                os.replace(staged, path)
            else:
                os.link(staged, path)
                os.unlink(staged)
            staged = None
            result["path"] = path
        if "error" not in result and operation == "task.logs" and args.output_dir:
            files = result.pop("files")
            directory = os.path.abspath(args.output_dir) if args.output_dir else tempfile.mkdtemp(prefix="task-diagnostics-", dir="/data")
            if args.output_dir:
                os.mkdir(directory)
            paths = []
            for name, content in files.items():
                if name not in {"status.json", "logs.jsonl", "history.jsonl"}:
                    raise ValueError("Unexpected diagnostic filename.")
                with open(os.path.join(directory, name), "x", encoding="utf-8") as handle:
                    handle.write(content)
                paths.append(os.path.join(directory, name))
            result.update(directory=directory, files=paths)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 1 if "error" in result else 0
    except (ValueError, OSError, RuntimeError, KeyboardInterrupt) as exc:
        unknown = dispatched and operation in WRITE_OPERATIONS
        result = api.uncertain_result() if unknown else api.error(
            "command_interrupted" if dispatched else "invalid_arguments", str(exc) or "Command interrupted.",
            "Task execution is independent of this listener. Inspect task status/list before retrying." if dispatched else "Run this command's --help; check input and output paths.")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 1 if dispatched or isinstance(exc, RuntimeError) else 2
    finally:
        if output is not None:
            output.close()
        if staged is not None:
            os.unlink(staged)
