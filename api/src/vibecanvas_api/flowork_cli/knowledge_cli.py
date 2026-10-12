"""Stdlib-only Knowledge CLI and shared untrusted request validation."""
import base64
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from uuid import UUID

READ_OPERATIONS = frozenset("knowledge." + name for name in ("list", "get", "download", "check"))
WRITE_OPERATIONS = frozenset("knowledge." + name for name in ("create", "publish", "delete"))
OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS

GIT_HELP = (
    " Packages are self-contained Git directories; .git is stored and downloaded with the package. "
    "Use git log and git diff to inspect history/changes. Before create/publish, initialize Git if needed, "
    "review and commit all intended files. HEAD must exist; staged, unstaged, untracked, ignored extra files "
    "and unresolved conflicts block publication. Nothing is auto-committed on publication. "
    "Keep temporary files outside the package. --expected-version still identifies the platform version, not a commit."
)


def package_error_hint(resource, action):
    hint = f"Read {resource} {action} --help."
    if action in {"create", "publish"}:
        return hint + " Inspect git status --ignored and git diff; review/commit intended changes and move unrelated files outside the package before publishing."
    if action == "download":
        return hint + " Downloads require a new directory; partial local files may remain after failure."
    return hint


def package_git(source, *args):
    # Do not inherit an enclosing repository or execute hooks/fsmonitor helpers.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", "-C", os.path.abspath(source), "-c", "core.hooksPath=" + os.devnull,
             "-c", "core.fsmonitor=false", "-c", "core.fileMode=false", *args],
            env=env, capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError as exc:
        raise ValueError("Git is required for Skill/Knowledge packages; install git first.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Package Git check exceeded 30 seconds; inspect the local repository.") from exc
    if result.returncode:
        raise ValueError("Package Git command failed: " + (result.stderr.strip() or " ".join(args)))
    return result.stdout


def inspect_package_git(source):
    root = Path(source).absolute()
    git_dir = root / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise ValueError("Package root needs its own .git directory. Use git init, review files, git add and git commit first; linked worktrees are not portable packages.")
    # A downloaded config is data, not permission to execute filters or read
    # another repository. Ordinary local/user/remote/branch settings are kept.
    config = package_git(root, "config", "--local", "--no-includes", "--name-only", "--list")
    for key in config.lower().splitlines():
        if key.startswith(("include.", "includeif.", "filter.")) or key in {
            "core.worktree", "extensions.worktreeconfig", "core.sshcommand",
        }:
            raise ValueError("Non-portable package Git configuration: " + key)
    for name in ("objects/info/alternates", "objects/info/http-alternates", "commondir", "shallow"):
        if (git_dir / name).exists():
            raise ValueError("Package must contain its complete Git history; unsupported .git/" + name)
    package_git(root, "rev-parse", "--verify", "HEAD^{commit}")
    if any(line.startswith("160000 ") for line in package_git(root, "ls-files", "--stage").splitlines()):
        raise ValueError("Git submodules are not self-contained package files; include the actual files instead.")
    package_git(root, "fsck", "--connectivity-only", "--no-reflogs")
    return package_git(root, "rev-parse", "HEAD").strip()


def require_clean_package(source):
    commit = inspect_package_git(source)
    if any(line[:1].islower() or line.startswith("S ") for line in package_git(source, "ls-files", "-v").splitlines()):
        raise ValueError("Clear Git assume-unchanged/skip-worktree flags before publication so every file is checked.")
    status = package_git(source, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching")
    if status:
        raise ValueError("Package has uncommitted, untracked or ignored files. Inspect git status and git diff, resolve conflicts and commit intended changes; move unrelated files outside the package. Nothing was uploaded.\n" + status)
    return commit


def prepare_download_git(source):
    root = Path(source)
    initialized = not (root / ".git").exists()
    if initialized:
        package_git(root, "init", "--quiet", "--template=", "--initial-branch=main")
        package_git(root, "config", "core.fileMode", "false")
        package_git(root, "add", "--force", "--all")
        package_git(root, "-c", "user.name=Flowork", "-c", "user.email=packages@flowork.local",
                    "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "Downloaded package baseline")
    commit = inspect_package_git(root)
    # The package transport stores regular-file bytes, not executable bits.
    package_git(root, "config", "core.fileMode", "false")
    return {"git_commit": commit, "git_initialized": initialized,
            "git_clean": not bool(package_git(root, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching")),
            "git_hint": "Use git log / git diff. Review and commit changes before create/publish; .git history is included. Existing history is preserved; legacy packages receive one local baseline commit."}


def add_parser(groups):
    def command(parent, name, description):
        return parent.add_parser(name, help=description, description=description, allow_abbrev=False)
    root = command(groups, "knowledge", "Manage versioned Knowledge packages. Download then use bash to inspect/search files. No mounted folder or refresh command. Read leaf --help before writing.")
    actions = root.add_subparsers(dest="action", required=True)
    descriptions = {
        "list": "List discoverable packages with pagination. Follow next_offset with --offset until null; pipe JSON through jq/grep to filter text. Discovery does not grant read/write permission.",
        "get": "Read published metadata, package_version, file paths/sizes and indexing states. Does not download file contents.",
        "download": "Download the latest published package. --output-dir must NOT exist; omission creates a unique directory under /data/knowledge. Never overwrites local edits. The downloaded README includes package metadata; downloading leaves remote content unchanged. Returns local_directory/readme/package_version/file_count; inspect files using bash.",
        "check": "Validate ALL files in --source-dir without saving or publishing. Root README.md requires YAML frontmatter with name (1..200 characters) and description (up to 2000 characters, may be empty). Only regular files; no symlinks. No third-party CLI dependency needed.",
        "create": "Create a new package from ALL files in --source-dir, including hidden files. Name/description come from root README.md YAML frontmatter. Run check first. Exclude private credentials. Returns knowledge_id and package_version. After an unknown outcome inspect list before retrying.",
        "publish": "Publish ALL files in --source-dir as a new version, including README.md name/description. Replaces the whole package; absent files are removed. --expected-version from download is required. Newer publications or unpublished drafts reject the write; download and merge before retrying. Approval freezes submitted bytes. Indexing is asynchronous; never republish to wait for indexing.",
        "delete": "Delete the remote package, preserving local downloads. Requires configured write approval; no bypass flag. Active indexing may block deletion; inspect get and wait before retrying.",
    }
    for action, description in descriptions.items():
        if action in {"download", "check", "create", "publish"}:
            description += GIT_HELP
        leaf = command(actions, action, description)
        if action in {"get", "download", "publish", "delete"}:
            leaf.add_argument("--knowledge-id", required=True, help="Exact Knowledge UUID from list/create.")
        if action == "list":
            leaf.add_argument("--limit", type=int, default=20)
            leaf.add_argument("--offset", type=int, default=0)
        if action in {"create", "check", "publish"}:
            leaf.add_argument("--source-dir", required=True, help="Directory of regular files; root README.md required. Limits: 256 files, 200 MiB total, 16 path levels.")
        if action == "publish":
            leaf.add_argument("--expected-version", type=int, required=True)
        if action == "download":
            leaf.add_argument("--output-dir")


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
        "list": {"limit", "offset"}, "get": {"knowledge_id"},
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
            raise ValueError("--knowledge-id is required and must be a UUID.")
        value["knowledge_id"] = str(UUID(identifier))
    if action == "list":
        value.setdefault("limit", 20)
        value.setdefault("offset", 0)
        if type(value["limit"]) is not int or not 1 <= value["limit"] <= 200:
            raise ValueError("--limit must be between 1 and 200.")
        if type(value["offset"]) is not int or value["offset"] < 0:
            raise ValueError("--offset must be non-negative.")
    if action == "publish" and "expected_version" not in value:
        raise ValueError("--expected-version from download is required.")
    if "expected_version" in value and (type(value["expected_version"]) is not int or value["expected_version"] < 1):
        raise ValueError("--expected-version must be a positive integer.")
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
    git_commit = None
    try:
        value = {k: v for k, v in vars(args).items() if v is not None and k not in {"resource", "action", "source_dir", "output_dir"}}
        if getattr(args, "source_dir", None):
            value["files"] = collect(args.source_dir)
            if args.action in {"create", "publish"}:
                git_commit = require_clean_package(args.source_dir)
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
        if git_commit and "error" not in result:
            result["git_commit"] = git_commit
        if destination and "error" not in result:
            files = result.pop("files")
            materialize(destination, files)
            result.update(prepare_download_git(destination))
            readme = next(item["path"] for item in files if item["path"].casefold() == "readme.md")
            result.update(local_directory=destination, readme=os.path.join(destination, readme))
    except (OSError, ValueError, KeyError, KeyboardInterrupt) as exc:
        if dispatched and operation in WRITE_OPERATIONS:
            result = cli.uncertain_result()
        else:
            result = cli.error("read_failed" if dispatched else "invalid_arguments", str(exc),
                package_error_hint("knowledge", args.action))
        if destination:
            result["local_directory"] = destination
    result.setdefault("status", "failed" if "error" in result else "succeeded")
    return cli.emit_result(result, exit_code=2 if result.get("error") == "invalid_arguments" else (1 if "error" in result else 0))
