"""Stdlib-only Knowledge CLI and shared untrusted request validation."""
import base64
import json
import os
import stat
import tempfile
from uuid import UUID

READ_OPERATIONS = frozenset("knowledge." + name for name in ("list", "status", "download", "search"))
WRITE_OPERATIONS = frozenset("knowledge." + name for name in ("create", "update", "upload", "delete"))
OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS


def add_parser(groups):
    def command(parent, name, description):
        return parent.add_parser(name, help=description, description=description, allow_abbrev=False)
    root = command(groups, "knowledge", "Manage authorized file-tree Knowledge packages. Stateless: use --knowledge_id. Read README.md after download. Saved files and asynchronous search indexing have separate states. Read leaf --help before writing.")
    actions = root.add_subparsers(dest="action", required=True)
    descriptions = {
        "list": "List discoverable Knowledge packages, not file contents. Defaults to 20 items; use next_offset. Discovery does not grant read/write permission.",
        "status": "Read package metadata, current package_version, file paths/sizes/index states and failures. Does not download file contents. index_status describes derived text search, not file availability.",
        "create": "Create a package, optionally from --source_dir. Without a source, generate README.md. With a source, publish ALL regular files recursively, including hidden files; root README.md is required. No ZIP input, symlinks or special files. Validate the whole package before creation. Returns knowledge_id and package_version; indexing is asynchronous. After an unknown outcome inspect list before retrying.",
        "update": "Update ONLY supplied --name/--description; at least one is required. --description '' clears it. Does not change files, README.md or package_version.",
        "download": "Download one consistent current package snapshot. --output_dir must NOT exist and its parent must exist; omission creates a unique directory under /data/knowledge. Never overwrites local edits. Returns local_directory/readme/package_version/file_count, not file contents on stdout. On local failure a partial directory may remain; use a new destination.",
        "upload": "Replace the ENTIRE remote package with ALL files in --source_dir, including hidden files. Files absent locally are REMOVED remotely. Requires root README.md; no symlinks or special files. No expected_version or force flag: each successful commit increments the server's current package_version. Concurrent publications serialize; the last commit wins. Uploading an old download overwrites intervening changes. Validation/failure before commit leaves the current package unchanged. This is not historical version browsing or rollback. Approval freezes the submitted bytes. Indexing is asynchronous; pending/failed indexing is NOT a reason to upload again.",
        "search": "Search the derived lexical text index, NOT the public web or all packages. Repeat --knowledge_id to search multiple explicit packages; --limit defaults to 5 (1..20, total across packages). Returns file paths and matching snippets. Incomplete indexing means missing matches do not prove absence; download and inspect raw files when necessary.",
        "delete": "Delete the remote package and its files, not local downloads. No --yes/--confirm bypass. agent/always_ask require live user approval; always_allow auto-approves. Refresh preserves pending approval only while this command lives. Active indexing may block deletion: inspect status instead of repeated retries.",
    }
    for action, description in descriptions.items():
        leaf = command(actions, action, description)
        if action not in {"list", "create"}:
            leaf.add_argument("--knowledge_id", required=True, action="append" if action == "search" else "store", help="Exact Knowledge package UUID from list/create; repeat only for search.")
        if action == "list":
            leaf.add_argument("--limit", type=int, default=20)
            leaf.add_argument("--offset", type=int, default=0)
        if action in {"create", "update"}:
            leaf.add_argument("--name", required=action == "create")
            leaf.add_argument("--description")
        if action in {"create", "upload"}:
            leaf.add_argument("--source_dir", required=action == "upload", help="Sandbox directory; relative paths use the shell's working directory. All files are published, so exclude private credentials yourself. Uses existing platform package rules (256 files, 200 MiB total, 16 path levels); no extra CLI payload cap.")
        if action == "download":
            leaf.add_argument("--output_dir")
        if action == "search":
            leaf.add_argument("--query", required=True)
            leaf.add_argument("--limit", type=int, default=5)


def safe_path(path):
    if (not isinstance(path, str) or not path or path.startswith("/") or "\\" in path
            or any(ord(c) < 32 for c in path) or any(p in {"", ".", ".."} for p in path.split("/"))):
        raise ValueError("Package files require portable relative paths without traversal.")
    return path


def validate_files(files):
    if not isinstance(files, list) or not files:
        raise ValueError("The package must contain a root README.md and regular files.")
    paths = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "data"}:
            raise ValueError("Invalid package file entry.")
        path = safe_path(item["path"]).casefold()
        if path in paths:
            raise ValueError("Duplicate package file path: " + item["path"])
        paths.add(path)
        if not isinstance(item["data"], str):
            raise ValueError("Invalid package file data.")
        base64.b64decode(item["data"], validate=True)
    for path in paths:
        if any("/".join(path.split("/")[:i]) in paths for i in range(1, len(path.split("/")))):
            raise ValueError("A package path cannot be both a file and a directory.")
    if "readme.md" not in paths:
        raise ValueError("Knowledge package root must contain README.md.")
    return files


def validate(operation, arguments):
    if operation not in OPERATIONS or not isinstance(arguments, dict):
        raise ValueError("Unsupported Knowledge operation or arguments.")
    action = operation.split(".")[1]
    allowed = {
        "list": {"limit", "offset"}, "status": {"knowledge_id"},
        "download": {"knowledge_id"}, "delete": {"knowledge_id"},
        "create": {"name", "description", "files"},
        "update": {"knowledge_id", "name", "description"},
        "upload": {"knowledge_id", "files"},
        "search": {"knowledge_id", "query", "limit"},
    }[action]
    if arguments.keys() - allowed:
        raise ValueError("Unsupported parameters: " + ", ".join(sorted(arguments.keys() - allowed)))
    value = dict(arguments)
    if action not in {"list", "create"}:
        ids = value.get("knowledge_id")
        if action != "search":
            ids = [ids]
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
            raise ValueError("--knowledge_id is required and must be a UUID.")
        ids = list(dict.fromkeys(str(UUID(i)) for i in ids))
        if action == "search" and len(ids) > 50:
            raise ValueError("Search supports at most 50 Knowledge packages.")
        value["knowledge_id"] = ids if action == "search" else ids[0]
    if action == "create" and "name" not in value:
        raise ValueError("--name is required.")
    if action == "update" and not ({"name", "description"} & value.keys()):
        raise ValueError("update requires --name or --description.")
    for key, maximum in (("name", 200), ("description", 2000), ("query", 2000)):
        if key in value:
            if not isinstance(value[key], str) or len(value[key]) > maximum or (key != "description" and not value[key].strip()):
                raise ValueError(f"Invalid --{key}; maximum {maximum} characters.")
    if "name" in value:
        value["name"] = value["name"].strip()
    if action == "search" and "query" not in value:
        raise ValueError("--query is required.")
    if action in {"list", "search"}:
        value.setdefault("limit", 20 if action == "list" else 5)
        maximum = 200 if action == "list" else 20
        if type(value["limit"]) is not int or not 1 <= value["limit"] <= maximum:
            raise ValueError(f"--limit must be between 1 and {maximum}.")
    if action == "list":
        value.setdefault("offset", 0)
        if type(value["offset"]) is not int or value["offset"] < 0:
            raise ValueError("--offset must be non-negative.")
    if action == "upload" and "files" not in value:
        raise ValueError("upload requires --source_dir.")
    if "files" in value:
        validate_files(value["files"])
    return value


def collect(source):
    files = []
    def walk(fd, prefix):
        for name in sorted(os.listdir(fd)):
            path = safe_path(prefix + name)
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                mode = os.fstat(child).st_mode
                if stat.S_ISDIR(mode):
                    walk(child, path + "/")
                elif stat.S_ISREG(mode):
                    with os.fdopen(os.dup(child), "rb") as stream:
                        files.append({"path": path, "data": base64.b64encode(stream.read()).decode("ascii")})
                else:
                    raise ValueError("Only regular files and directories are supported: " + path)
            finally:
                os.close(child)
    fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(fd, "")
    finally:
        os.close(fd)
    return validate_files(files)


def materialize(root, files):
    # Use directory descriptors throughout: never follow a replaced path/symlink.
    validate_files(files)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for item in files:
            parts = item["path"].split("/")
            fd = os.dup(root_fd)
            try:
                for part in parts[:-1]:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                target = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                with os.fdopen(target, "wb") as stream:
                    stream.write(base64.b64decode(item["data"], validate=True))
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(fd)
        if not os.path.samestat(os.fstat(root_fd), os.stat(root, follow_symlinks=False)):
            raise OSError("Download directory changed during execution.")
    finally:
        os.close(root_fd)


def execute(args, endpoint, cli):
    operation = "knowledge." + args.action
    dispatched = False
    destination = None
    try:
        value = {k: v for k, v in vars(args).items() if v is not None and k not in {"resource", "action", "source_dir", "output_dir"}}
        if getattr(args, "source_dir", None):
            value["files"] = collect(args.source_dir)
        value = validate(operation, value)
        if not endpoint:
            raise ValueError("No active Flowork Agent execution is connected.")
        if args.action == "download":
            output = getattr(args, "output_dir", None)
            if output:
                destination = os.path.abspath(output)
                os.mkdir(destination, 0o700)
            else:
                os.makedirs("/data/knowledge", mode=0o700, exist_ok=True)
                destination = tempfile.mkdtemp(prefix=value["knowledge_id"] + "-", dir="/data/knowledge")
        dispatched = True
        result = cli.request(endpoint, value, operation=operation)
        if destination and "error" not in result:
            files = result.pop("files")
            materialize(destination, files)
            readme = next(item["path"] for item in files if item["path"].casefold() == "readme.md")
            result.update(local_directory=destination, readme=os.path.join(destination, readme))
    except (OSError, ValueError, KeyError, KeyboardInterrupt) as exc:
        if dispatched and operation in WRITE_OPERATIONS:
            result = cli.uncertain_result()
        else:
            result = cli.error("read_failed" if dispatched else "invalid_arguments", str(exc),
                "Check knowledge " + args.action + " --help. Downloads require a new destination; partial local files may remain after failure.")
        if destination:
            result["local_directory"] = destination
    result.setdefault("status", "failed" if "error" in result else "succeeded")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 2 if result.get("error") == "invalid_arguments" else (1 if "error" in result else 0)
