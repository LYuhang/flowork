"""Stdlib-only discovery of workflow Skills and MCP installations."""
import json
from uuid import UUID

try:
    from . import skill_cli
except ImportError:
    import skill_cli

OPERATIONS = frozenset({"skill.list", "skill.get",
                        "mcp.list", "mcp.get", "mcp.tools"})


def add_parser(groups):
    for resource, actions in (("skill", ("list", "get")),
                              ("mcp", ("list", "get", "tools"))):
        root = groups.add_parser(resource, help=f"Discover authorized {resource} installations for SubAgent nodes.", allow_abbrev=False)
        commands = root.add_subparsers(dest="action", required=True)
        if resource == "skill":
            skill_cli.add_commands(commands)
        for action in actions:
            leaf = commands.add_parser(action, allow_abbrev=False,
                description="Read resource metadata or definitions. Does not invoke MCP business tools. IDs refer to installed resources, not catalog entries.")
            if action == "list":
                leaf.add_argument("--search", default="")
                leaf.add_argument("--limit", type=int, default=20)
                leaf.add_argument("--offset", type=int, default=0)
            else:
                leaf.add_argument("--skill_id" if resource == "skill" else "--server_id", required=True)


def validate(operation, arguments):
    if operation not in OPERATIONS or not isinstance(arguments, dict):
        raise ValueError("Unsupported resource discovery operation.")
    resource, action = operation.split(".")
    identifier = "skill_id" if resource == "skill" else "server_id"
    allowed = {"search", "limit", "offset"} if action == "list" else {identifier}
    if arguments.keys() - allowed:
        raise ValueError("Unexpected resource discovery parameters.")
    value = dict(arguments)
    if action != "list":
        if not isinstance(value.get(identifier), str):
            raise ValueError(f"--{identifier} requires an installation UUID.")
        value[identifier] = str(UUID(value[identifier]))
    if action == "list":
        value.setdefault("offset", 0)
        value.setdefault("limit", 20)
        maximum = 100
        if type(value["offset"]) is not int or not 0 <= value["offset"] <= 2147483647:
            raise ValueError("--offset must be a non-negative integer.")
        if type(value["limit"]) is not int or not 1 <= value["limit"] <= maximum:
            raise ValueError(f"--limit must be between 1 and {maximum}.")
    if action == "list":
        value.setdefault("search", "")
        if not isinstance(value["search"], str) or len(value["search"]) > 500:
            raise ValueError("--search must be text of at most 500 characters.")
    return value


def execute(args, endpoint, cli):
    operation = f"{args.resource}.{args.action}"
    try:
        value = validate(operation, {key: val for key, val in vars(args).items()
            if key not in {"resource", "action"} and val is not None})
        if not endpoint:
            raise ValueError("No active Flowork Agent execution is connected.")
        result = cli.request(endpoint, value, operation=operation)
    except (ValueError, OSError) as exc:
        result = cli.error("invalid_arguments", str(exc), f"Read flowork-cli {operation.replace('.', ' ')} --help.")
    result.setdefault("status", "failed" if "error" in result else "succeeded")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 2 if result.get("error") == "invalid_arguments" else (1 if "error" in result else 0)
