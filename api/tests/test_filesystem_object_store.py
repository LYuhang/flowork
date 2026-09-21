"""Cross-process blob sharing through ``FilesystemObjectStore``.

The default ``inmemory`` store keeps blobs in a process-global dict, so a
blob ``put_bytes`` by the API process is invisible to the DBOS worker
process — KB file indexing (api puts blob → worker fetches it) raises
``KeyError``, and batch result download is broken cross-container.

``FilesystemObjectStore`` writes blobs to a directory shared between
API + background worker (a Docker named volume in Compose;
a shared dir in native multi-process runs). LangFlow/Dify-style local
backend before S3.

The cross-process property is modelled here by creating TWO separate
``FilesystemObjectStore`` instances pointed at the same root dir — two
instances == two processes sharing a mounted directory. The blob written
by the first is fetchable by the second. THIS is the proof the bug is
fixed; see :func:`test_cross_instance_fetch_is_the_fix`.
"""
from __future__ import annotations

import os
import base64

import pytest

from vibecanvas_api.services.object_store import (
    FilesystemObjectStore,
    get_object_store,
)
from vibecanvas_api.security.object_cipher import MAGIC
from vibecanvas_api.security.secret_service import SecretIntegrityError


def test_put_then_fetch_roundtrips_identical_bytes(tmp_path):
    store = FilesystemObjectStore(root=str(tmp_path))
    data = b"hello,world\n\x00\x01binary"
    store.put_bytes("kb/t1/k1/doc.txt", data, content_type="text/plain")
    assert store.fetch_bytes("kb/t1/k1/doc.txt") == data
    durable_path = tmp_path / "kb" / "t1" / "k1" / "doc.txt"
    durable = durable_path.read_bytes()
    assert durable.startswith(MAGIC)
    assert data not in durable
    assert durable_path.stat().st_mode & 0o777 == 0o660
    assert (tmp_path / "kb").stat().st_mode & 0o777 == 0o770
    assert (tmp_path / "kb" / "t1" / "k1").stat().st_mode & 0o777 == 0o770


def test_legacy_owner_only_ciphertext_permissions_are_repaired(tmp_path):
    legacy_dir = tmp_path / "run" / "tenant" / "run-id"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "result.json"
    legacy_file.write_bytes(b"legacy")
    legacy_dir.chmod(0o700)
    legacy_file.chmod(0o600)

    FilesystemObjectStore(root=str(tmp_path))

    assert legacy_dir.stat().st_mode & 0o777 == 0o770
    assert legacy_file.stat().st_mode & 0o777 == 0o660
    assert (tmp_path / ".service-group-permissions-v1").exists()


def test_range_read_decrypts_exact_requested_bytes(tmp_path):
    store = FilesystemObjectStore(root=str(tmp_path), encryption_chunk_bytes=64 * 1024)
    data = bytes(range(256)) * 2048
    store.put_bytes("artifacts/t/scope/data.bin", data)
    actual = b"".join(store.iter_bytes(
        "artifacts/t/scope/data.bin",
        start=63_900,
        end=131_211,
        chunk_size=17_000,
    ))
    assert actual == data[63_900:131_212]


def test_plaintext_file_is_rejected_by_strict_runtime(tmp_path):
    path = tmp_path / "old.txt"
    path.write_bytes(b"retired plaintext storage")
    store = FilesystemObjectStore(root=str(tmp_path))
    with pytest.raises(SecretIntegrityError, match="not encrypted"):
        store.fetch_bytes("old.txt")


def test_fetch_missing_key_raises_keyerror(tmp_path):
    """Match :class:`InMemoryObjectStore`'s contract so callers behave
    identically regardless of backend."""
    store = FilesystemObjectStore(root=str(tmp_path))
    with pytest.raises(KeyError):
        store.fetch_bytes("kb/t1/k1/does-not-exist.txt")


def test_cross_instance_fetch_is_the_fix(tmp_path):
    """THE point of this task — a SECOND instance (== a second process)
    pointed at the same root dir can fetch what the FIRST instance wrote.

    With ``InMemoryObjectStore`` this would raise ``KeyError`` because each
    process owns its own dict; with the filesystem backend the shared
    directory IS the shared state, so it round-trips.
    """
    writer = FilesystemObjectStore(root=str(tmp_path))  # e.g. the API process
    reader = FilesystemObjectStore(root=str(tmp_path))  # e.g. the worker process
    payload = b"%PDF-1.4 fake kb upload"
    key = "kb/tenant-abc/kb-xyz/upload.pdf"
    writer.put_bytes(key, payload, content_type="application/pdf")
    # Distinct instance, no shared in-process dict — only the dir is shared.
    assert reader.fetch_bytes(key) == payload


def test_delete_prefix_removes_matching_leaves_non_matching(tmp_path):
    store = FilesystemObjectStore(root=str(tmp_path))
    store.put_bytes("kb/t1/kbA/a.txt", b"a")
    store.put_bytes("kb/t1/kbA/sub/b.txt", b"b")
    store.put_bytes("kb/t1/kbB/c.txt", b"c")  # non-matching prefix
    store.delete_prefix("kb/t1/kbA/")
    with pytest.raises(KeyError):
        store.fetch_bytes("kb/t1/kbA/a.txt")
    with pytest.raises(KeyError):
        store.fetch_bytes("kb/t1/kbA/sub/b.txt")
    # The sibling KB is untouched.
    assert store.fetch_bytes("kb/t1/kbB/c.txt") == b"c"


def test_delete_prefix_missing_is_noop(tmp_path):
    """Best-effort by contract — deleting a non-existent prefix is silent."""
    store = FilesystemObjectStore(root=str(tmp_path))
    store.delete_prefix("kb/nope/")  # must not raise


def test_put_bytes_returns_fs_uri(tmp_path):
    """URI scheme is ``fs://{key}`` so the key is recoverable from the URI
    (the download route maps URI → key for non-S3 streaming)."""
    store = FilesystemObjectStore(root=str(tmp_path))
    uri = store.put_bytes("tasks/t1/results.csv", b"i,output\n0,ok\n")
    assert uri == "fs://tasks/t1/results.csv"


def test_signed_url_raises_notimplemented(tmp_path):
    """Filesystem has no native HTTP URL — fail loudly rather than hand
    back a dead redirect target. The download route streams via
    ``fetch_bytes`` for non-S3 providers instead."""
    store = FilesystemObjectStore(root=str(tmp_path))
    uri = store.put_bytes("tasks/t1/results.csv", b"x")
    with pytest.raises(NotImplementedError):
        store.signed_url(uri)


# ---------------------------------------------------------------- security

# ``_path`` must never let a key
# escape ``root`` (path traversal), and ``delete_prefix`` must never be
# able to nuke the whole store via an empty / root-equivalent prefix.


def test_put_bytes_rejects_traversal_key(tmp_path):
    """A ``..``-laden key must NOT write outside ``root`` — it must raise
    and leave no file at the escaped location (FIX-1: defense-in-depth in
    ``_path``)."""
    store = FilesystemObjectStore(root=str(tmp_path / "fs_root"))
    os.makedirs(str(tmp_path / "fs_root"), exist_ok=True)
    escape_target = tmp_path / "evil-marker.txt"
    # ../evil-marker.txt resolves to a sibling of fs_root → outside root.
    with pytest.raises((ValueError, KeyError)):
        store.put_bytes("../evil-marker.txt", b"pwned")
    assert not escape_target.exists(), "traversal escaped the store root"


def test_fetch_bytes_rejects_traversal_key(tmp_path):
    """``fetch_bytes`` with a traversal key must raise (not read an
    arbitrary file outside ``root``)."""
    root = tmp_path / "fs_root"
    os.makedirs(str(root), exist_ok=True)
    # Plant a secret OUTSIDE the root that the traversal would target.
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top-secret")
    store = FilesystemObjectStore(root=str(root))
    with pytest.raises((ValueError, KeyError)):
        store.fetch_bytes("../secret.txt")


def test_normal_key_still_roundtrips_after_guard(tmp_path):
    """The containment guard must not break a normal nested key."""
    store = FilesystemObjectStore(root=str(tmp_path))
    store.put_bytes("kb/t/k/f/report.csv", b"a,b\n1,2\n")
    assert store.fetch_bytes("kb/t/k/f/report.csv") == b"a,b\n1,2\n"


@pytest.mark.parametrize("bad", ["", ".", "/", "  ", "./"])
def test_delete_prefix_rejects_empty_or_root(tmp_path, bad):
    """An empty / root-equivalent prefix must raise — never walk + unlink
    the entire store root (FIX-3)."""
    store = FilesystemObjectStore(root=str(tmp_path))
    store.put_bytes("kb/t/k/keep.txt", b"keep")
    with pytest.raises(ValueError):
        store.delete_prefix(bad)
    # Existing key survives the rejected call.
    assert store.fetch_bytes("kb/t/k/keep.txt") == b"keep"


def test_delete_prefix_normal_still_works(tmp_path):
    """A real prefix still deletes matching keys + leaves siblings (FIX-3
    guard must not regress the normal path)."""
    store = FilesystemObjectStore(root=str(tmp_path))
    store.put_bytes("kb/t/k/a.txt", b"a")
    store.put_bytes("kb/t/k/sub/b.txt", b"b")
    store.put_bytes("kb/t/other/c.txt", b"c")
    store.delete_prefix("kb/t/k/")
    with pytest.raises(KeyError):
        store.fetch_bytes("kb/t/k/a.txt")
    with pytest.raises(KeyError):
        store.fetch_bytes("kb/t/k/sub/b.txt")
    assert store.fetch_bytes("kb/t/other/c.txt") == b"c"


@pytest.mark.parametrize(
    "raw,expected_safe",
    [
        ("report.csv", "report.csv"),
        ("../../etc/passwd", "passwd"),
        ("../../../../tmp/evil", "evil"),
        ("a/b/c.txt", "c.txt"),
        ("..", "unnamed"),
        (".", "unnamed"),
        ("", "unnamed"),
        (None, "unnamed"),
        ("  ", "unnamed"),
        ("my file (1).pdf", "my file (1).pdf"),
    ],
)
def test_safe_object_key_segment(raw, expected_safe):
    """FIX-1 source-layer: the user-controlled upload filename is reduced
    to a single safe path segment (no separators, no ``..``) before it is
    embedded in an object_store key. Normal filenames are preserved."""
    from vibecanvas_api.services.object_store import safe_object_key_segment

    out = safe_object_key_segment(raw)
    assert "/" not in out
    assert "\\" not in out
    assert out not in ("", ".", "..")
    assert out == expected_safe


def test_safe_segment_embedded_key_does_not_traverse(tmp_path):
    """A sanitized segment used in a real object key must round-trip inside
    the store root even when the original filename was a traversal attack."""
    from vibecanvas_api.services.object_store import safe_object_key_segment

    store = FilesystemObjectStore(root=str(tmp_path / "fs_root"))
    seg = safe_object_key_segment("../../etc/passwd")
    key = f"kb/t/k/f/{seg}"
    store.put_bytes(key, b"data")
    # The blob lands under root, recoverable by the same key.
    assert store.fetch_bytes(key) == b"data"
    assert not (tmp_path / "etc" / "passwd").exists()


def test_get_object_store_filesystem_provider(tmp_path, monkeypatch):
    """``OBJECT_STORE_PROVIDER=filesystem`` → a ``FilesystemObjectStore``
    rooted at ``OBJECT_STORE_FS_ROOT``."""
    from vibecanvas_api.config import ObjectStoreConfig, config as app_config

    monkeypatch.setenv("OBJECT_STORE_PROVIDER", "filesystem")
    monkeypatch.setenv("OBJECT_STORE_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(
        app_config,
        "kms_local_master_key",
        base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
    )
    monkeypatch.setattr(app_config, "kms_local_master_key_file", "")
    # Rebuild the object-store config from env so the new provider/root
    # take effect on the live singleton.
    monkeypatch.setattr(app_config, "object_store", ObjectStoreConfig({}))
    store = get_object_store()
    assert isinstance(store, FilesystemObjectStore)
    # And it actually works against the configured root.
    store.put_bytes("probe.txt", b"ok")
    assert FilesystemObjectStore(root=str(tmp_path)).fetch_bytes("probe.txt") == b"ok"
