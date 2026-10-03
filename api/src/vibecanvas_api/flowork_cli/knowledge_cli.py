"""Stdlib-only Knowledge CLI and shared untrusted request validation."""
import base64
import os
import stat
import tempfile
from uuid import UUID

READ_OPERATIONS = frozenset("knowledge." + name for name in ("list", "get", "download", "check"))
WRITE_OPERATIONS = frozenset("knowledge." + name for name in ("create", "publish", "delete"))
OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS


def add_parser(groups):
    def command(parent, name, description):
        return parent.add_parser(name, help=description, description=description, allow_abbrev=False)
    root = command(groups, "knowledge", "Manage versioned Knowledge packages. Download then use bash to inspect/search files. No mounted folder or refresh command. Read leaf --help before writing.")
    actions = root.add_subparsers(dest="action", required=True)
    descriptions = {
        "list": "List discoverable packages with pagination. --search filters names/descriptions, not file contents. Discovery does not grant read/write permission.",
        "get": "Read published metadata, package_version, file paths/sizes and indexing states. Does not download file contents.",
        "download": "Download the latest published package. --output_dir must NOT exist; omission creates a unique directory under /data/knowledge. Never overwrites local edits. Legacy README gets metadata frontmatter in the local copy only. Returns local_directory/readme/package_version/file_count; inspect files using bash.",
        "check": "Validate ALL files in --source_dir without saving or publishing. Root README.md requires YAML frontmatter with name (1..200 characters) and description (up to 2000 characters, may be empty). Only regular files; no symlinks. No third-party CLI dependency needed.",
        "create": "Create a new package from ALL files in --source_dir, including hidden files. Name/description come from root README.md YAML frontmatter. Run check first. Exclude private credentials. Returns knowledge_id and package_version. After an unknown outcome inspect list before retrying.",
        "publish": "Publish ALL files in --source_dir as a new version, including README.md name/description. Replaces the whole package and any draft; absent files are removed. --expected_version rejects publication if the current version differs (recommended after download). Without it, last commit wins. Approval freezes submitted bytes. Indexing is asynchronous; never republish to wait for indexing.",
        "delete": "Delete the remote package, preserving local downloads. Requires configured write approval; no bypass flag. Active indexing may block deletion; inspect get and wait before retrying.",
    }
    for action, description in descriptions.items():
        leaf = command(actions, action, description)
        if action in {"get", "download", "publish", "delete"}:
            leaf.add_argument("--knowledge_id", required=True, help="Exact Knowledge UUID from list/create.")
        if action == "list":
            leaf.add_argument("--limit", type=int, default=20)
            leaf.add_argument("--offset", type=int, default=0)
            leaf.add_argument("--search", help="Filter package names and descriptions.")
        if action in {"create", "check", "publish"}:
            leaf.add_argument("--source_dir", required=True, help="Directory of regular files; root README.md required. Limits: 256 files, 200 MiB total, 16 path levels.")
        if action == "publish":
            leaf.add_argument("--expected_version", type=int)
        if action == "download":
            leaf.add_argument("--output_dir")


def safe_path(path):
    if (not isinstance(path, str) or not path or path.startswith("/") or "\\" in path
            or any(ord(c) < 32 for c in path) or any(p in {"", ".", ".."} for p in path.split("/"))):
        raise ValueError("Package files require portable relative paths without traversal.")
    return path


def validate_files(files, *, entrypoint="README.md"):
    if not isinstance(files, list) or not files:
        raise ValueError(f"The package must contain a root {entrypoint} and regular files.")
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
    if entrypoint.casefold() not in paths:
        raise ValueError(f"Package root must contain {entrypoint}.")
    return files


def validate(operation, arguments):
    if operation not in OPERATIONS or not isinstance(arguments, dict):
        raise ValueError("Unsupported Knowledge operation or arguments.")
    action = operation.split(".")[1]
    allowed = {
        "list": {"limit", "offset", "search"}, "get": {"knowledge_id"},
        "download": {"knowledge_id"}, "delete": {"knowledge_id"},
        "create": {"files"}, "check": {"files"},
        "publish": {"knowledge_id", "files", "expected_version"},
    }[action]
    if arguments.keys() - allowed:
        raise ValueError("Unsupported parameters: " + ", ".join(sorted(arguments.keys() - allowed)))
    value = dict(arguments)
    if action in {"get", "download", "publish", "delete"}:
        identifier = value.get("knowledge_id")
        if not isinstance(identifier, str):
            raise ValueError("--knowledge_id is required and must be a UUID.")
        value["knowledge_id"] = str(UUID(identifier))
    if action == "list":
        value.setdefault("limit", 20)
        value.setdefault("offset", 0)
        if type(value["limit"]) is not int or not 1 <= value["limit"] <= 200:
            raise ValueError("--limit must be between 1 and 200.")
        if type(value["offset"]) is not int or value["offset"] < 0:
            raise ValueError("--offset must be non-negative.")
        if "search" in value and (not isinstance(value["search"], str) or len(value["search"]) > 2000):
            raise ValueError("--search must be text up to 2000 characters.")
    if "expected_version" in value and (type(value["expected_version"]) is not int or value["expected_version"] < 1):
        raise ValueError("--expected_version must be a positive integer.")
    if action in {"create", "check", "publish"}:
        validate_files(value.get("files"))
    return value


def collect(source, *, entrypoint="README.md"):
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
    return validate_files(files, entrypoint=entrypoint)


def materialize(root, files, *, entrypoint="README.md"):
    # Use directory descriptors throughout: never follow a replaced path/symlink.
    validate_files(files, entrypoint=entrypoint)
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
    return cli.emit_result(result, exit_code=2 if result.get("error") == "invalid_arguments" else (1 if "error" in result else 0))
