"""Offline tests for the native Codex packaging boundary, not browser acceptance."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("codex_builder", ROOT / "scripts/build_codex_runtime.py")
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@pytest.fixture
def vendor(tmp_path, monkeypatch):
    path = tmp_path / "vendor"
    path.mkdir()
    manifest = {"layoutVersion": 1, "version": builder.VERSION,
                "target": "x86_64-unknown-linux-musl", "variant": "codex",
                "entrypoint": "bin/codex", "resourcesDir": "codex-resources",
                "pathDir": "codex-path"}
    (path / "codex-package.json").write_text(json.dumps(manifest))
    for name in ("bin/codex", "bin/codex-code-mode-host", "codex-path/rg",
                 "codex-resources/bwrap", "codex-resources/zsh/bin/zsh"):
        item = path / name
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_bytes(b"fixture")
        item.chmod(0o755)
    monkeypatch.setattr(builder.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(builder.platform, "system", lambda: "Linux")
    return path


def test_vendor_requires_complete_same_version_bundle(vendor):
    assert builder.validate_vendor(vendor)["version"] == builder.VERSION
    (vendor / "bin/codex-code-mode-host").unlink()
    with pytest.raises(ValueError, match="Missing executable companion"):
        builder.validate_vendor(vendor)


@pytest.mark.parametrize("key,value", [("version", "0.146.0"), ("target", "aarch64-unknown-linux-musl"),
                                      ("entrypoint", "../codex"), ("resourcesDir", "/etc")])
def test_vendor_rejects_wrong_layout_or_release(vendor, key, value):
    marker = vendor / "codex-package.json"
    data = json.loads(marker.read_text())
    data[key] = value
    marker.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="official same-version"):
        builder.validate_vendor(vendor)


def test_vendor_cannot_smuggle_external_files(vendor, tmp_path):
    (vendor / "outside").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="external symlink"):
        builder.validate_vendor(vendor)


def test_existing_output_never_overwritten(tmp_path):
    dest = tmp_path / "installed"
    dest.mkdir()
    (dest / "keep").write_text("existing runtime")
    with pytest.raises(ValueError, match="already exists"):
        builder.build(tmp_path, tmp_path, dest, 4)
    assert (dest / "keep").read_text() == "existing runtime"


def test_broken_destination_symlink_not_followed(tmp_path):
    dest = tmp_path / "installed"
    dest.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="already exists"):
        builder.build(tmp_path, tmp_path, dest, 4)
    assert dest.is_symlink()


def test_nested_output_cannot_recursively_copy_itself(tmp_path):
    with pytest.raises(ValueError, match="outside the source and vendor"):
        builder.build(tmp_path, tmp_path, tmp_path / "nested-output", 4)


@pytest.fixture
def source(tmp_path, monkeypatch):
    paths = list(builder.SOURCES)
    hashes = {name: values[1] for name, values in builder.SOURCES.items()}
    hashes[builder.LOCK] = next(iter(builder.LOCK_HASHES))
    def output(args, cwd):
        assert cwd == tmp_path
        if args[1] == "rev-parse":
            return builder.COMMIT
        if args[1] == "diff":
            return "\n".join(paths)
        return ""
    monkeypatch.setattr(builder, "output", output)
    monkeypatch.setattr(builder, "digest", lambda path: hashes[str(path.relative_to(tmp_path))])
    return tmp_path, hashes, paths


def test_only_exact_reviewed_source_and_lock_allowed(source):
    root, hashes, _ = source
    builder.validate_source(root, patched=True)
    hashes[builder.LOCK] = "unreviewed dependency update"
    with pytest.raises(ValueError, match="Unexpected Cargo.lock"):
        builder.validate_source(root)


def test_unrelated_changes_are_rejected(source):
    root, _, paths = source
    paths.append("codex-rs/core/src/other.rs")
    with pytest.raises(ValueError, match="unrelated tracked changes"):
        builder.validate_source(root)


def test_partially_applied_patch_rejected(source):
    root, hashes, _ = source
    name = next(iter(builder.SOURCES))
    hashes[name] = builder.SOURCES[name][0]
    with pytest.raises(ValueError, match="incomplete or missing"):
        builder.validate_source(root)


def test_source_tampering_inside_allowed_file_rejected(source):
    root, hashes, _ = source
    hashes[next(iter(builder.SOURCES))] = "another edit in the same file"
    with pytest.raises(ValueError, match="Unexpected source contents"):
        builder.validate_source(root)


def test_build_packages_companions_and_provenance_without_installing(vendor, tmp_path, monkeypatch):
    source = tmp_path / "source"
    binary = source / "codex-rs/target/x86_64-unknown-linux-gnu/release/codex"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"built native executable")
    binary.chmod(0o755)
    for name in ("LICENSE", "NOTICE"):
        (source / name).write_text(name + " from upstream")
    for name in builder.SOURCES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("already patched fixture")
    calls = []
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    monkeypatch.setattr(builder, "validate_source", lambda *args, **kwargs: None)
    monkeypatch.setattr(builder, "output", lambda args, cwd: (
        "rustc 1.95.0 (fixture)" if args[0] == "rustc" else f"codex-cli {builder.VERSION}"))
    monkeypatch.setattr(builder, "run", lambda args, cwd: calls.append(args))
    monkeypatch.setattr(builder.subprocess, "run", lambda *args, **kwargs: None)
    destination = tmp_path / "bundle"
    result = builder.build(source, vendor, destination, 2)
    assert result["status"] == "succeeded"
    assert calls == [["cargo", "build", "-p", "codex-cli", "--release", "--locked",
                      "--target", "x86_64-unknown-linux-gnu", "--jobs", "2"],
                     ["strip", "--strip-debug", str(destination / "bin/codex")]]
    assert (vendor / "bin/codex").read_bytes() == b"fixture"
    assert (destination / "bin/codex").read_bytes() == binary.read_bytes()
    assert (destination / "bin/codex-code-mode-host").read_bytes() == b"fixture"
    assert (destination / "codex-resources/zsh/bin/zsh").read_bytes() == b"fixture"
    manifest = json.loads((destination / "codex-package.json").read_text())
    assert manifest["floworkSourceCommit"] == builder.COMMIT
    assert manifest["floworkPatch"] == builder.PATCH_ID
    assert manifest["floworkFilesSha256"]["bin/codex"] == result["sha256"]
    for name, sha256 in manifest["floworkFilesSha256"].items():
        assert builder.digest(destination / name) == sha256
    assert manifest["floworkFilesSha256"]["NOTICE"] == builder.digest(source / "NOTICE")
    assert builder.verify_bundle(destination)["sha256"] == result["sha256"]
    nested_marker = destination / "bin/codex-package.json"
    nested_marker.write_text("{}")
    with pytest.raises(ValueError, match="inventory"):
        builder.verify_bundle(destination)
    nested_marker.unlink()
    (destination / "bin/alias").symlink_to("codex")
    with pytest.raises(ValueError, match="inventory|symlinks"):
        builder.verify_bundle(destination)
    (destination / "bin/alias").unlink()
    (destination / "bin/codex-code-mode-host").write_bytes(b"modified companion")
    with pytest.raises(ValueError, match="checksum mismatch"):
        builder.verify_bundle(destination)


def test_managed_bundle_cannot_be_an_official_unpatched_bundle(vendor):
    with pytest.raises(ValueError, match="provenance"):
        builder.verify_bundle(vendor)


@pytest.mark.parametrize("value", [None, [], "invalid"])
def test_non_object_manifest_fails_with_actionable_error(vendor, value):
    (vendor / "codex-package.json").write_text(json.dumps(value))
    for validate in (builder.verify_bundle, builder.validate_vendor):
        with pytest.raises(ValueError, match="must be an object"):
            validate(vendor)
