import os
import tempfile
from pathlib import Path

import pytest

from vibecanvas_api.security.object_cipher import MAGIC
from vibecanvas_api.services.object_store import (
    FilesystemObjectStore,
    InMemoryObjectStore,
)


def test_filesystem_materialize_prefix_returns_real_dir_with_bytes():
    with tempfile.TemporaryDirectory() as root:
        st = FilesystemObjectStore(root=root)
        st.put_bytes("run/t1/r1/a.txt", b"hello", "text/plain")
        st.put_bytes("run/t1/r1/sub/b.bin", b"\x89PNG\x00", "image/png")
        d = st.materialize_prefix("run/t1/r1/")
        assert os.path.isdir(d)
        with open(os.path.join(d, "a.txt"), "rb") as f:
            assert f.read() == b"hello"
        with open(os.path.join(d, "sub", "b.bin"), "rb") as f:
            assert f.read() == b"\x89PNG\x00"
        assert Path(root, "run/t1/r1/a.txt").read_bytes()[:8] == MAGIC


def test_put_updates_active_plaintext_materialization_without_exposing_key(tmp_path):
    store = FilesystemObjectStore(root=str(tmp_path / "cipher"))
    directory = store.materialize_prefix("run/t1/r1/")
    store.put_bytes("run/t1/r1/live.txt", b"v1")
    assert Path(directory, "live.txt").read_bytes() == b"v1"
    store.put_bytes("run/t1/r1/live.txt", b"v2")
    assert Path(directory, "live.txt").read_bytes() == b"v2"
    assert (tmp_path / "cipher" / "run/t1/r1/live.txt").read_bytes() != b"v2"


def test_hot_reads_use_active_materialization_without_redecrypting(tmp_path, monkeypatch):
    store = FilesystemObjectStore(root=str(tmp_path / "cipher"))
    key = "run/t1/r1/live.txt"
    store.put_bytes(key, b"durable-old")
    directory = store.materialize_prefix("run/t1/r1/")
    materialized = os.path.join(directory, "live.txt")
    with open(materialized, "wb") as file:
        file.write(b"sandbox-current")

    def forbidden_decrypt(*_args, **_kwargs):
        raise AssertionError("hot materialized reads must not decrypt VCOBJ2")

    monkeypatch.setattr(store._cipher, "read", forbidden_decrypt)
    monkeypatch.setattr(store._cipher, "iter_range", forbidden_decrypt)

    assert store.fetch_bytes(key) == b"sandbox-current"
    assert b"".join(store.iter_bytes(key, start=8, end=14, chunk_size=3)) == b"current"


def test_writeback_preserves_live_inode_and_encrypts_snapshot(tmp_path):
    store = FilesystemObjectStore(root=str(tmp_path / "cipher"))
    key = "run/t1/r1/results.jsonl"
    directory = store.materialize_prefix("run/t1/r1/")
    path = Path(directory, "results.jsonl")
    with path.open("wb") as writer:
        inode = os.fstat(writer.fileno()).st_ino
        store.persist_materialized_bytes(key, b"")
        writer.write(b'{"y":7}\n')
        writer.flush()
        store.persist_materialized_bytes(key, b"")  # delayed older snapshot
        assert path.stat().st_ino == inode
        assert path.read_bytes() == b'{"y":7}\n'
        writer.write(b'{"y":9}\n')
        writer.flush()
        store.persist_materialized_bytes(key, path.read_bytes())
    assert (tmp_path / "cipher" / key).read_bytes().startswith(MAGIC)
    store.release_materialized_prefix("run/t1/r1/", directory)
    assert store.fetch_bytes(key) == b'{"y":7}\n{"y":9}\n'


def test_release_materialized_prefix_keeps_ciphertext_only(tmp_path):
    store = FilesystemObjectStore(root=str(tmp_path / "cipher"))
    key = "run/t1/r1/private.txt"
    store.put_bytes(key, b"plaintext")
    directory = store.materialize_prefix("run/t1/r1")
    assert os.path.exists(os.path.join(directory, "private.txt"))

    store.release_materialized_prefix("run/t1/r1", directory)

    assert not os.path.exists(directory)
    assert store.fetch_bytes(key) == b"plaintext"


def test_inmemory_materialize_raises():
    st = InMemoryObjectStore()
    with pytest.raises(NotImplementedError):
        st.materialize_prefix("run/t1/r1/")


def test_delete_bytes_removes_exactly_one_key_inmemory():
    s = InMemoryObjectStore()
    s.put_bytes("a/img_1.png", b"\x89PNG\x01")
    s.put_bytes("a/img_10.png", b"\x89PNG\x02")   # sibling sharing the "img_1" basename prefix
    s.delete_bytes("a/img_1.png")
    with pytest.raises(KeyError):
        s.fetch_bytes("a/img_1.png")
    assert s.fetch_bytes("a/img_10.png") == b"\x89PNG\x02"   # sibling untouched
    s.delete_bytes("a/missing.png")                          # missing key = silent no-op


def test_delete_bytes_removes_file_filesystem(tmp_path):
    s = FilesystemObjectStore(root=str(tmp_path))
    s.put_bytes("a/img_1.png", b"\x89PNG\x01")
    s.delete_bytes("a/img_1.png")                            # regression guard vs silent no-op
    with pytest.raises(KeyError):
        s.fetch_bytes("a/img_1.png")
    s.delete_bytes("a/missing.png")                          # missing = silent no-op
