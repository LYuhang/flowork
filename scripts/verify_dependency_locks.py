#!/usr/bin/env python3
"""Validate Flowork dependency lock invariants without third-party packages."""

from __future__ import annotations

import json
import re
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^ \\]+)")


def normalized(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def pins(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", "--hash=")):
            continue
        match = PIN.match(line)
        if match:
            result[normalized(match.group(1))] = match.group(2)
            continue
        if line.startswith(("-e ", "-r ", "-c ")) or line.endswith("\\"):
            continue
        raise SystemExit(
            f"{path.relative_to(ROOT)}:{line_number}: unpinned requirement: {raw}"
        )
    return result


def validate_runtime_lock(path: Path, development: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8")
    locked = pins(path)
    if not locked:
        raise SystemExit(f"{path.relative_to(ROOT)} contains no pinned packages")
    if "--hash=sha256:" not in text:
        raise SystemExit(f"{path.relative_to(ROOT)} contains no package hashes")
    mismatch = {
        name: (version, development.get(name))
        for name, version in locked.items()
        if development.get(name) != version
    }
    if mismatch:
        raise SystemExit(
            f"{path.relative_to(ROOT)} differs from requirements-dev.txt: {mismatch}"
        )


def validate_sandbox_lock(development: dict[str, str]) -> None:
    """Keep the shared Agent/Workflow baseline exact, hashed, and compatible."""

    input_path = ROOT / "requirements-sandbox.in"
    lock_path = ROOT / "requirements-sandbox.txt"
    requested = pins(input_path)
    locked = validate_hashed_lock(lock_path)
    missing = sorted(set(requested) - set(locked))
    if missing:
        raise SystemExit(f"requirements-sandbox.txt is missing direct pins: {missing}")
    changed = {
        name: (version, locked.get(name))
        for name, version in requested.items()
        if locked.get(name) != version
    }
    if changed:
        raise SystemExit(
            f"requirements-sandbox.txt differs from requirements-sandbox.in: {changed}"
        )
    conflicts = {
        name: (version, development[name])
        for name, version in locked.items()
        if name in development and development[name] != version
    }
    if conflicts:
        raise SystemExit(
            f"requirements-sandbox.txt conflicts with requirements-dev.txt: {conflicts}"
        )


def validate_hashed_lock(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    locked = pins(path)
    if not locked:
        raise SystemExit(f"{path.relative_to(ROOT)} contains no pinned packages")
    if "--hash=sha256:" not in text:
        raise SystemExit(f"{path.relative_to(ROOT)} contains no package hashes")
    return locked


def validate_build_locks() -> None:
    root_build = validate_hashed_lock(ROOT / "requirements-build.txt")
    engine_build = validate_hashed_lock(ROOT / "engine/requirements-build.txt")
    if root_build != engine_build:
        raise SystemExit("root and Engine build-tool locks differ")
    required = {
        "editables": "0.5",
        "hatchling": "1.31.0",
        "pip": "26.2.1",
    }
    if any(root_build.get(name) != version for name, version in required.items()):
        raise SystemExit(f"build-tool lock must contain exact pins: {required}")
    for relative in ("api/pyproject.toml", "engine/pyproject.toml"):
        pyproject = tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))
        if pyproject["build-system"]["requires"] != ["hatchling==1.31.0"]:
            raise SystemExit(
                f"{relative} must pin the reviewed Hatchling build backend"
            )


def validate_node_lock(package_dir: str) -> None:
    directory = ROOT / package_dir
    package = json.loads((directory / "package.json").read_text(encoding="utf-8"))
    manager = package.get("packageManager", "")
    if not re.fullmatch(r"pnpm@\d+\.\d+\.\d+", manager):
        raise SystemExit(
            f"{package_dir}/package.json must pin packageManager to an exact pnpm version"
        )
    if not (directory / "pnpm-lock.yaml").is_file():
        raise SystemExit(f"{package_dir}/pnpm-lock.yaml is missing")
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, version in package.get(section, {}).items():
            if not isinstance(version, str) or not re.fullmatch(
                r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version
            ):
                raise SystemExit(
                    f"{package_dir}/package.json {section}.{name} must use an exact version; got {version!r}"
                )


def main() -> None:
    development = pins(ROOT / "requirements-dev.txt")
    validate_runtime_lock(ROOT / "requirements-runtime.txt", development)
    validate_runtime_lock(ROOT / "engine/requirements-runtime.txt", development)
    validate_sandbox_lock(development)
    validate_build_locks()
    validate_node_lock("web")
    validate_node_lock("extension")
    print("Dependency locks are pinned and consistent.")


if __name__ == "__main__":
    main()
