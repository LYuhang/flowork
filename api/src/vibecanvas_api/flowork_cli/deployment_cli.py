"""Stdlib-only Deployment CLI. Never imports host credentials or database code."""
from datetime import datetime
import base64
import json
import os
import re
from uuid import UUID

READ_OPERATIONS = frozenset("deployment." + name for name in ("list", "info", "status", "history", "logs"))
WRITE_OPERATIONS = frozenset("deployment." + name for name in (
    "create", "update", "enable", "disable", "run", "rotate_key", "delete"))
OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS

_CREDENTIAL_FILE_HELP = (
    "The credential file is JSON, not a raw token, regardless of its filename. "
    "It contains deployment_id and api_key (API) or hmac_secret (webhook). "
    "For API calls, parse the file as JSON and use only api_key as the Bearer token; "
    "never send the whole file as a token. Read it inside the HTTP client process, "
    "not into command arguments or logs. "
    "POST the StartNode input object directly to the returned endpoint with "
    "Content-Type: application/json; do not wrap it in an inputs property. "
    "For example, --inputs '{\"batch_id\":\"example\",\"orders\":[]}' becomes "
    "the HTTP body {\"batch_id\":\"example\",\"orders\":[]}. "
    "The response contains final EndNode business values directly in outputs."
)


def add_parser(groups):
    def command(parent, name, description):
        return parent.add_parser(name, help=description, description=description, allow_abbrev=False)
    root = command(groups, "deployment", "Manage published Workflow entry points. Stateless, named arguments only. Creation does not execute. Deployment status is not execution success. Read leaf --help before acting.")
    actions = root.add_subparsers(dest="action", required=True)
    listing = command(actions, "list", "List authorized deployments. Optional Workflow filter; defaults to 20 items. Use next_offset for another page. No trigger/status/query filters.")
    listing.add_argument("--workflow_id")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--offset", type=int, default=0)
    for action, description in {
        "info": "Read deployment configuration, enabled status and public endpoint. Never returns credentials or proves execution health. " + _CREDENTIAL_FILE_HELP,
        "status": "Read exactly one invocation status, timestamps and error. Requires --execution_id from run/history; never selects the latest implicitly.",
        "logs": "Read one invocation workflow event log, oldest first. Requires --execution_id. Use --after CURSOR for the next page. These are persisted workflow events, not sandbox stdout. Older invocations may have no recorded trace; logs_available reports this explicitly.",
        "enable": "Enable future calls after approval; does not execute the Workflow.",
        "disable": "Disable new calls. Does not guarantee cancellation of in-flight execution or undo side effects.",
        "delete": "Soft-delete and disable the deployment, not its Workflow. Does not undo in-flight side effects. Approval applies.",
        "rotate_key": "API deployments only. Old API key immediately stops working. Save the one-time replacement to --secret_file; never automatically rotate again after an unknown result.",
        "run": "Make one REAL test call, like the page Test action. Not a dry run. Uses current platform permission, not an API key. Returns final outputs/errors when completed promptly, or HTTP 202 with execution_id and execution_url after human approval is reached or the observation wait expires. The admitted execution continues independently. Inspect that execution through status or its detail link; do not run again to poll. Workflow business timeouts still apply. Tests execution, not external key/signature/network reachability.",
        "history": "List invocation history newest first. Use next_cursor as --after. Exports default to the last seven days; override with --from/--to. --output_dir exports invocations.jsonl and history.json with paging metadata to a NEW directory. Use status/logs --execution_id for one invocation.",
        "create": "Create an enabled deployment by default; does not execute. Required --version selects a fixed saved snapshot. --secret_file is a NEW sandbox file for the one-time API key/webhook secret (0600); stdout never prints it. After an unknown result inspect list before retrying. --slug defaults to the lowercased name with non-ASCII/alphanumeric groups replaced by hyphens (fallback deployment); collisions are errors.",
        "update": "Update ONLY supplied settings, not Workflow ID, trigger type or slug. Version changes and increased execution exposure may require approval. Use enable/disable for availability. Existing accepted calls retain their frozen version/mount.",
    }.items():
        leaf = command(actions, action, description)
        if action != "create":
            leaf.add_argument("--deployment_id", required=True, help="Deployment ID returned by create/list, never an execution ID.")
        if action in {"create", "update"}:
            leaf.add_argument("--name", required=action == "create")
            version = leaf.add_mutually_exclusive_group(required=action == "create")
            version.add_argument("--version", help="Fixed saved version, e.g. v2.sv3. Never infers a target from Chat state.")
            leaf.add_argument("--rate_limit_qps", type=int, help="Non-negative soft QPS cap, default 10 on create. 0 disables this rate limit, not a capacity guarantee.")
            leaf.add_argument("--mount", choices=("true", "false"), help="Expose deployment owner's authorized /mount. Default false on create; omission preserves on update. Never shares Chat /data or /memory.")
        if action == "create":
            leaf.add_argument("--workflow_id", required=True)
            leaf.add_argument("--trigger_type", required=True, choices=("api", "webhook"))
            leaf.add_argument("--slug")
            leaf.add_argument("--enabled", choices=("true", "false"), default="true")
        if action in {"create", "rotate_key"}:
            leaf.add_argument("--secret_file", required=True, help="Required NEW local file; parent must exist. Keep private, do not preview/share. No overwrite option. The file is reserved before dispatch and may remain empty after rejection or an unknown result. Only credential_saved=true confirms a usable credential was written; existence alone does not. " + _CREDENTIAL_FILE_HELP)
        if action == "run":
            inputs = leaf.add_mutually_exclusive_group()
            inputs.add_argument("--inputs", help="JSON object matching StartNode input names; default {}.")
            inputs.add_argument("--inputs_file", help="Read the input JSON object from a sandbox file.")
        if action in {"status", "logs"}:
            leaf.add_argument("--execution_id", required=True, help="Exact invocation ID from run/history.")
        if action == "logs":
            leaf.add_argument("--after", type=int, default=0, help="Exclusive event sequence cursor, default 0.")
            leaf.add_argument("--limit", type=int, default=100)
        if action == "history":
            leaf.add_argument("--status", choices=("queued", "running", "waiting_approval", "succeeded", "failed", "timed_out", "cancelled"))
            leaf.add_argument("--from", dest="from_time", help="ISO-8601 with timezone.")
            leaf.add_argument("--to", dest="to_time", help="ISO-8601 with timezone.")
            leaf.add_argument("--limit", type=int)
            leaf.add_argument("--after")
            leaf.add_argument("--output_dir", help="New directory containing history.json (paging metadata) and invocations.jsonl (invocation summaries).")


def validate(operation, arguments):
    if operation not in OPERATIONS or not isinstance(arguments, dict):
        raise ValueError("Unsupported Deployment operation or arguments.")
    target = {"deployment_id"}
    settings = {"name", "major", "version", "rate_limit_qps", "mount"}
    allowed = {
        "list": {"workflow_id", "limit", "offset"}, "info": target, "status": target | {"execution_id"},
        "logs": target | {"execution_id", "after", "limit"},
        "history": target | {"status", "from_time", "to_time", "limit", "after", "export"},
        "create": settings | {"workflow_id", "trigger_type", "slug", "enabled"},
        "update": settings | target, "enable": target, "disable": target,
        "delete": target, "rotate_key": target, "run": target | {"inputs"},
    }
    action = operation.split(".")[1]
    if arguments.keys() - allowed[action]:
        raise ValueError("Unsupported parameters: " + ", ".join("--" + k for k in sorted(arguments.keys() - allowed[action])))
    value = dict(arguments)
    if action not in {"list", "create"} and "deployment_id" not in value:
        raise ValueError("--deployment_id is required.")
    if action in {"status", "logs"} and not value.get("execution_id"):
        raise ValueError("--execution_id is required; use info for configuration or history to discover executions.")
    for key in ("deployment_id", "execution_id"):
        if key in value:
            if not isinstance(value[key], str):
                raise ValueError(f"--{key} must be a UUID.")
            value[key] = str(UUID(value[key]))
    for key in ("workflow_id", "name"):
        if key in value:
            if not isinstance(value[key], str) or not value[key].strip():
                raise ValueError(f"--{key} cannot be empty.")
            value[key] = value[key].strip()
    if "name" in value and len(value["name"]) > 200:
        raise ValueError("--name must contain 1 to 200 characters.")
    if action == "create":
        if not value.get("workflow_id") or not value.get("name") or value.get("trigger_type") not in ("api", "webhook"):
            raise ValueError("create requires --workflow_id, --name and --trigger_type api|webhook.")
        value.setdefault("enabled", True)
        value.setdefault("mount", False)
        value.setdefault("rate_limit_qps", 10)
        value.setdefault("slug", re.sub("[^a-z0-9]+", "-", value["name"].lower()).strip("-")[:63] or "deployment")
    if "slug" in value and (not isinstance(value["slug"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value["slug"])):
        raise ValueError("--slug must contain 1 to 63 lowercase ASCII letters, digits or hyphens; no leading hyphen.")
    for key in ("enabled", "mount", "export"):
        if key in value and type(value[key]) is not bool:
            raise ValueError(f"--{key} must be true or false.")
    if "major" in value:
        raise ValueError("Deployments require a fixed --version, e.g. v2.sv3; --major is no longer supported.")
    if action == "create" and not value.get("version"):
        raise ValueError("create requires --version.")
    for key, pattern in (("major", r"v[1-9]\d*"), ("version", r"v[1-9]\d*\.sv\d+")):
        if key in value and (not isinstance(value[key], str) or not re.fullmatch(pattern, value[key]) or value[key].endswith("sv")):
            raise ValueError(f"Invalid --{key}. Use v2 for a major or v2.sv3 for a fixed version.")
    if action == "update" and not (settings & value.keys()):
        raise ValueError("update requires at least one setting.")
    for key, minimum, maximum in (("limit", 1, 200), ("offset", 0, None), ("rate_limit_qps", 0, None)):
        if key in value and (type(value[key]) is not int or value[key] < minimum or (maximum is not None and value[key] > maximum)):
            raise ValueError(f"Invalid --{key}.")
    if action == "status" and value.get("execution_id") and value.keys() & {"status", "from_time", "to_time", "limit", "after"}:
        raise ValueError("--execution_id cannot be combined with paging, status or time filters.")
    for key in ("from_time", "to_time"):
        if key in value:
            dt = datetime.fromisoformat(value[key].replace("Z", "+00:00"))
            if dt.utcoffset() is None:
                raise ValueError("Time filters require an explicit timezone.")
    if value.get("from_time") and value.get("to_time") and datetime.fromisoformat(value["from_time"].replace("Z", "+00:00")) > datetime.fromisoformat(value["to_time"].replace("Z", "+00:00")):
        raise ValueError("--from must not be after --to.")
    if "status" in value and value["status"] not in {"queued", "running", "waiting_approval", "succeeded", "failed", "timed_out", "cancelled"}:
        raise ValueError("Unsupported execution status.")
    if action == "logs" and (type(value.get("after", 0)) is not int or value.get("after", 0) < 0):
        raise ValueError("--after must be a non-negative event sequence.")
    if "after" in value and action == "history":
        try:
            cursor = json.loads(base64.urlsafe_b64decode(value["after"]).decode())
            UUID(cursor["id"])
            if datetime.fromisoformat(cursor["submitted_at"]).utcoffset() is None:
                raise ValueError("Missing timezone")
        except Exception as exc:
            raise ValueError("--after requires a cursor returned by history.") from exc
    if action == "run":
        value.setdefault("inputs", {})
        if not isinstance(value["inputs"], dict):
            raise ValueError("Inputs must be a JSON object.")
    return value


def execute(args, endpoint, cli):
    operation = "deployment." + args.action
    destination = getattr(args, "secret_file", None)
    output_dir = getattr(args, "output_dir", None)
    descriptor = None
    dispatched = False
    saved = False
    result = {}
    try:
        value = {k: v for k, v in vars(args).items() if v is not None and k not in {
            "resource", "action", "secret_file", "output_dir", "inputs_file"}}
        for key in ("mount", "enabled"):
            if key in value:
                value[key] = value[key] == "true"
        if "inputs" in value:
            value["inputs"] = cli.strict_json(value["inputs"])
        if getattr(args, "inputs_file", None):
            value["inputs"] = cli.read_workflow(args.inputs_file)
        if output_dir:
            value["export"] = True
        value = validate(operation, value)
        if not endpoint:
            raise ValueError("No active Flowork Agent execution is connected.")
        if destination:
            destination = os.path.abspath(destination)
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        if output_dir:
            output_dir = os.path.abspath(output_dir)
            os.mkdir(output_dir, 0o700)
        dispatched = True
        result = cli.request(endpoint, value, operation=operation)
        credential = result.pop("_credential", None)
        if credential is not None:
            if descriptor is None:
                raise OSError("No credential destination was reserved.")
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                json.dump(credential, stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
                if not os.path.samestat(os.fstat(stream.fileno()), os.stat(destination, follow_symlinks=False)):
                    raise OSError("Credential destination changed during execution.")
            saved = True
            result.update(secret_file=destination, credential_saved=True)
            result["hint"] = " ".join(filter(None, (_CREDENTIAL_FILE_HELP, result.get("hint"))))
        if output_dir and "files" in result:
            files = result.pop("files")
            paths = []
            for name, content in files.items():
                if name not in {"history.json", "invocations.jsonl"}:
                    raise ValueError("Unexpected diagnostic filename.")
                path = os.path.join(output_dir, name)
                with open(path, "x", encoding="utf-8") as stream:
                    stream.write(content)
                paths.append(path)
            result.update(output_dir=output_dir, files=paths)
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        if result.get("deployment_id") and destination and not saved:
            result.pop("_credential", None)
            result.update(error="credential_write_failed", credential_saved=False,
                message="The operation committed, but the credential file could not be saved completely.",
                hint="Do not create or rotate again automatically. Inspect this deployment and ask the user how to recover its credential.")
        elif dispatched and operation in WRITE_OPERATIONS:
            result = cli.uncertain_result()
        else:
            result = cli.error("invalid_arguments" if not dispatched else "read_failed", str(exc), "Check deployment " + args.action + " --help. Existing files are never overwritten.")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        # Reserved files belong to this command; retain them on uncertain
        # outcomes so a later command cannot silently overwrite evidence.
    if destination and not saved and dispatched:
        result.update(secret_file=destination, credential_saved=False)
    result.pop("_credential", None)
    code = 2 if result.get("error") == "invalid_arguments" else (1 if "error" in result or (args.action == "run" and result.get("status") in {"failed", "timed_out", "cancelled"}) else 0)
    return cli.emit_result(result, exit_code=code,
        state_field="execution_status" if args.action in {"run", "status", "logs"} else "resource_status")
