#!/usr/bin/env python3
"""Build the pinned output-receipt fix and preserve the official native bundle.

Requires Python 3.11+, Git, Cargo/Rust 1.95.0, a checkout of the pinned Codex
commit, and the same-version official npm platform vendor directory. This does
not install toolchains, replace system executables, or restart services. Build
logs go to stderr; the final bundle path and digest go to stdout as JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

VERSION = "0.147.0"
COMMIT = "be6e8eac029b183056b7e4402879f15d2c85f61b"
PATCH_ID = "output-subscription-1"
PATCH = Path(__file__).parent / "patches/codex-0.147.0-output-subscription.candidate.patch"
# Exact source/lock allowlist: reusing a build directory must not package an
# unrelated edit, a different dependency graph, or a partially applied patch.
SOURCES = {
    "codex-rs/core/src/unified_exec/process.rs": (
        "cc4a304fe888d57cc4b979364590dc2834a6469391fe3dc60d855fffa81dfaf1",
        "0b71a8868f73b285c148e9db2e54ac2659f40af105dfa4b82b409a4ec2697d45",
    ),
    "codex-rs/core/src/unified_exec/async_watcher_tests.rs": (
        "c75c072e540c34d1eeb395e1c12f74d1df52fa3da25f1f4192f48c54699996ff",
        "477bafcf178f378eea2750d85bb9c2f54fff3a39986322fe529d323caf0d4678",
    ),
}
LOCK = "codex-rs/Cargo.lock"
LOCK_HASHES = {
    "eeab4e9d3466da54037032251e2f13ad1ed11eae18bb8ee7dd2c89dbb86f645d",
    "bc4fe450de929afe82928734f860ca83e5f9dc5f9f1211b0974ea47b57af77ca",
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def output(args: list[str], cwd: Path) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, stdout=sys.stderr)


def validate_source(source: Path, *, patched: bool = False) -> None:
    if output(["git", "rev-parse", "HEAD"], source) != COMMIT:
        raise ValueError(f"Codex source must be pinned to {COMMIT}.")
    paths = output(["git", "diff", "HEAD", "--name-only"], source).splitlines()
    if set(paths) - {*SOURCES, LOCK}:
        raise ValueError("Codex source contains unrelated tracked changes.")
    if output(["git", "ls-files", "--others", "--exclude-standard"], source):
        raise ValueError("Codex source contains untracked files; use an isolated checkout.")
    states = set()
    for name, hashes in SOURCES.items():
        actual = digest(source / name)
        if actual not in hashes:
            raise ValueError(f"Unexpected source contents: {name}")
        states.add(hashes.index(actual))
    if len(states) != 1 or (patched and states != {1}):
        raise ValueError("The output-subscription patch is incomplete or missing.")
    if digest(source / LOCK) not in LOCK_HASHES:
        raise ValueError("Unexpected Cargo.lock; external dependency changes are not permitted.")


def validate_vendor(vendor: Path) -> dict:
    manifest = json.loads((vendor / "codex-package.json").read_text())
    if not isinstance(manifest, dict):
        raise ValueError("Codex package manifest must be an object.")
    architecture = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(platform.machine())
    if platform.system() != "Linux" or not architecture:
        raise ValueError("This builder supports native Linux x86_64/aarch64 only.")
    if any(manifest.get(key) != value for key, value in {
        "layoutVersion": 1, "version": VERSION, "variant": "codex",
        "entrypoint": "bin/codex", "resourcesDir": "codex-resources",
        "pathDir": "codex-path", "target": f"{architecture}-unknown-linux-musl",
    }.items()):
        raise ValueError("Use the official same-version, same-architecture Codex vendor bundle.")
    for name in ("bin/codex", "bin/codex-code-mode-host", "codex-path/rg",
                 "codex-resources/bwrap", "codex-resources/zsh/bin/zsh"):
        path = vendor / name
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"Missing executable companion: {name}")
    for path in vendor.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(vendor):
            raise ValueError("Vendor bundle contains an external symlink.")
    return manifest


def build(source: Path, vendor: Path, destination: Path, jobs: int) -> dict:
    if destination.exists() or destination.is_symlink():
        raise ValueError("Output directory already exists; choose a new versioned directory.")
    if any(destination.resolve().is_relative_to(root) for root in (source, vendor)):
        raise ValueError("Output directory must be outside the source and vendor trees.")
    if jobs < 1:
        raise ValueError("--jobs must be positive.")
    # A user-wide target override could otherwise silently select a different
    # architecture or a stale artifact. Use an explicit native target below.
    if os.environ.get("CARGO_TARGET_DIR"):
        raise ValueError("Unset CARGO_TARGET_DIR before building this bundle.")
    manifest = validate_vendor(vendor)
    validate_source(source)
    rust = output(["rustc", "--version"], source)
    if not rust.startswith("rustc 1.95.0 "):
        raise ValueError("Rust 1.95.0 is required by this pinned source build.")
    if digest(source / next(iter(SOURCES))) == next(iter(SOURCES.values()))[0]:
        run(["git", "apply", "--check", str(PATCH.resolve())], source)
        run(["git", "apply", str(PATCH.resolve())], source)
    crate = source / "codex-rs"
    # The release tag's lock has local workspace versions 0.0.0. Cargo changes
    # those to 0.147.0; only the exact reviewed normalization is accepted.
    subprocess.run(["cargo", "metadata", "--format-version", "1"], cwd=crate,
                   check=True, stdout=subprocess.DEVNULL)
    validate_source(source, patched=True)
    target = manifest["target"].replace("-musl", "-gnu")
    run(["cargo", "build", "-p", "codex-cli", "--release", "--locked",
         "--target", target, "--jobs", str(jobs)], crate)
    binary = crate / "target" / target / "release/codex"
    if output([str(binary), "--version"], source) != f"codex-cli {VERSION}":
        raise ValueError("Built executable has an unexpected version.")
    validate_source(source, patched=True)
    # A failed build never publishes a bundle. A packaging failure leaves the
    # explicit destination for inspection, not a half-installed system binary.
    shutil.copytree(vendor, destination)
    shutil.copy2(binary, destination / "bin/codex")
    # Upstream release retains >1 GiB of debug sections. Keep the original
    # compiler artifact for diagnosis; strip only the distributable copy.
    run(["strip", "--strip-debug", str(destination / "bin/codex")], source)
    for name in ("LICENSE", "NOTICE"):
        shutil.copy2(source / name, destination / name)
    manifest.update(target=target, floworkPatch=PATCH_ID, floworkSourceCommit=COMMIT,
                    floworkRustVersion=rust, floworkPatchSha256=digest(PATCH),
                    floworkCompanionTarget=validate_vendor(vendor)["target"])
    manifest["floworkFilesSha256"] = {
        str(path.relative_to(destination)): digest(path)
        for path in sorted(destination.rglob("*"))
        if path.is_file() and path != destination / "codex-package.json"
    }
    (destination / "codex-package.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"status": "succeeded", "executable": str(destination / "bin/codex"),
            "version": VERSION, "patch": PATCH_ID, "sha256": digest(destination / "bin/codex"),
            "message": "Bundle built. Run native-output and sandbox acceptance checks before deployment."}


def verify_bundle(bundle: Path) -> dict:
    """Check the managed deployment bundle without rebuilding or modifying it."""
    manifest = json.loads((bundle / "codex-package.json").read_text())
    if not isinstance(manifest, dict):
        raise ValueError("Codex package manifest must be an object.")
    target = {"x86_64": "x86_64-unknown-linux-gnu", "aarch64": "aarch64-unknown-linux-gnu"}.get(platform.machine())
    expected = {"layoutVersion": 1, "version": VERSION, "variant": "codex",
                "target": target, "entrypoint": "bin/codex", "resourcesDir": "codex-resources",
                "pathDir": "codex-path", "floworkPatch": PATCH_ID,
                "floworkSourceCommit": COMMIT, "floworkPatchSha256": digest(PATCH)}
    if platform.system() != "Linux" or target is None or any(manifest.get(k) != v for k, v in expected.items()):
        raise ValueError("Managed Codex bundle provenance, layout or architecture does not match this deployment.")
    hashes = manifest.get("floworkFilesSha256")
    files = {str(path.relative_to(bundle)): path for path in bundle.rglob("*")
             if path.is_file() and path != bundle / "codex-package.json"}
    if not isinstance(hashes, dict) or set(hashes) != set(files):
        raise ValueError("Managed Codex bundle file inventory is incomplete or changed.")
    for path in bundle.rglob("*"):
        if path.is_symlink():
            raise ValueError("Managed Codex bundle must contain files, not symlinks.")
    for name, path in files.items():
        if digest(path) != hashes[name]:
            raise ValueError(f"Managed Codex bundle checksum mismatch: {name}")
    for name in ("bin/codex", "bin/codex-code-mode-host", "codex-path/rg",
                 "codex-resources/bwrap", "codex-resources/zsh/bin/zsh"):
        if name not in files or not os.access(files[name], os.X_OK):
            raise ValueError(f"Missing executable companion: {name}")
    if output([str(bundle / "bin/codex"), "--version"], bundle) != f"codex-cli {VERSION}":
        raise ValueError("Managed Codex executable failed its version check.")
    return {"status": "succeeded", "executable": str(bundle / "bin/codex"),
            "version": VERSION, "patch": PATCH_ID, "sha256": hashes["bin/codex"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify_bundle", type=Path,
                        help="Verify an existing managed bundle only; do not build or install anything.")
    parser.add_argument("--source_dir", type=Path,
                        help=f"Isolated Codex checkout at {COMMIT}; this builder applies its pinned patch.")
    parser.add_argument("--vendor_dir", type=Path,
                        help=f"Official @openai/codex platform vendor directory for {VERSION}.")
    parser.add_argument("--output_dir", type=Path,
                        help="New bundle directory; existing paths are never overwritten.")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if args.verify_bundle and any((args.source_dir, args.vendor_dir, args.output_dir)):
        parser.error("--verify_bundle cannot be combined with build paths")
    if not args.verify_bundle and not all((args.source_dir, args.vendor_dir, args.output_dir)):
        parser.error("building requires --source_dir, --vendor_dir and --output_dir")
    try:
        result = (verify_bundle(args.verify_bundle.resolve()) if args.verify_bundle else
                  build(args.source_dir.resolve(), args.vendor_dir.resolve(), args.output_dir.absolute(), args.jobs))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(json.dumps({"status": "failed", "message": str(error)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
