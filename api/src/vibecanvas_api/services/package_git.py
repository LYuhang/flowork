"""Create a reviewed web publication without mutating the saved draft/history."""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

from vibecanvas_api.flowork_cli.knowledge_cli import (
    inspect_package_git, package_git, prepare_download_git, require_clean_package, safe_path,
)

PackageFiles = list[tuple[str, str | None, bytes]]


def is_git_metadata(path: str) -> bool:
    return path.replace("\\", "/").strip("/").split("/", 1)[0].casefold() == ".git"


def commit_web_package(base: PackageFiles, draft: PackageFiles, *, message: str) -> PackageFiles:
    """Use published Git history and commit exactly the draft's content files.

    Legacy packages receive a baseline of the previous publication first. All
    work happens in a temporary directory; callers persist the returned files
    and clear the draft in their existing database transaction.
    """
    with tempfile.TemporaryDirectory(prefix="flowork-package-") as directory:
        root = Path(directory)

        def write(files: PackageFiles) -> None:
            for path, _, data in files:
                target = root / safe_path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)

        write(base)
        if (root / ".git").exists():
            inspect_package_git(root)
        else:
            prepare_download_git(root)
        # Retain only the published history. Deletions in the draft must also
        # be reflected in the new tree, including directory/file replacements.
        for child in root.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        content = [item for item in draft if not is_git_metadata(item[0])]
        write(content)
        package_git(root, "add", "--force", "--all")
        package_git(root, "-c", "user.name=Flowork", "-c", "user.email=packages@flowork.local",
                    "-c", "commit.gpgSign=false", "commit", "--quiet", "--allow-empty", "-m", message)
        tree = {}
        for entry in package_git(root, "ls-tree", "-rz", "HEAD").split("\0"):
            if entry:
                header, path = entry.split("\t", 1)
                tree[path] = header.split()[2]
        for path, _, data in content:
            oid = tree.get(path, "")
            digest = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
            if oid != digest(f"blob {len(data)}\0".encode() + data).hexdigest():
                raise ValueError(f"Git attributes or index settings changed {path}; publish through the CLI after reviewing the file bytes.")
        require_clean_package(root)
        metadata = [(path.relative_to(root).as_posix(), "application/octet-stream", path.read_bytes())
                    for path in sorted((root / ".git").rglob("*")) if path.is_file()]
        return content + metadata
