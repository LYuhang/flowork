from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from vibecanvas_api.security.object_cipher import MAGIC
from vibecanvas_api.services.object_store import FilesystemObjectStore
from vibecanvas_api.services.vfs_volume import (
    EncryptedObjectStoreProjectRuntimeVolumeProvider,
    LocalPosixProjectRuntimeVolumeProvider,
)


def test_project_runtime_volume_is_direct_and_durable_across_provider_loss(tmp_path):
    provider = LocalPosixProjectRuntimeVolumeProvider(str(tmp_path))
    first = provider.ensure(
        tenant_id="tenant-one",
        user_id="user-one",
        project_scope_id="project-one",
    )
    agents = Path(first.path) / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text("keep this guidance", encoding="utf-8")
    database = Path(first.path) / ".codex" / "state.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('first turn')")
        connection.commit()

    # Recreating the provider models losing the sandbox/session process. There
    # is no hydrate step: the next process receives the exact same directory.
    second = LocalPosixProjectRuntimeVolumeProvider(str(tmp_path)).ensure(
        tenant_id="tenant-one",
        user_id="user-one",
        project_scope_id="project-one",
    )

    assert second == first
    assert agents.read_text(encoding="utf-8") == "keep this guidance"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM state").fetchone() == (
            "first turn",
        )
    assert os.stat(first.path).st_mode & 0o777 == 0o700


def test_project_runtime_volume_isolated_and_exact_delete(tmp_path):
    provider = LocalPosixProjectRuntimeVolumeProvider(str(tmp_path))
    first = provider.ensure(
        tenant_id="tenant", user_id="user", project_scope_id="project-one"
    )
    sibling = provider.ensure(
        tenant_id="tenant", user_id="user", project_scope_id="project-two"
    )
    Path(first.path, "marker").write_text("one", encoding="utf-8")
    Path(sibling.path, "marker").write_text("two", encoding="utf-8")

    assert provider.delete(
        tenant_id="tenant", user_id="user", project_scope_id="project-one"
    ) is True
    assert not Path(first.path).exists()
    assert Path(sibling.path, "marker").read_text(encoding="utf-8") == "two"
    assert provider.delete(
        tenant_id="tenant", user_id="user", project_scope_id="project-one"
    ) is False


def test_project_runtime_volume_does_not_import_legacy_chat_state(tmp_path):
    tenant = "tenant"
    user = "user"
    chat_scope = "chat-scope"
    legacy_scope = hashlib.sha256(
        (
            "vibecanvas:codex-state:v2\0"
            f"{tenant}\0{user}\0{chat_scope}"
        ).encode()
    ).hexdigest()
    legacy = tmp_path / tenant / user / "codex-chats-v2" / legacy_scope
    legacy.mkdir(parents=True)
    (legacy / "thread.jsonl").write_text("first reply", encoding="utf-8")

    volume = LocalPosixProjectRuntimeVolumeProvider(str(tmp_path)).ensure(
        tenant_id=tenant, user_id=user, project_scope_id="project-scope"
    )

    assert "project-runtime-v1" in volume.path
    assert not Path(volume.path, "thread.jsonl").exists()
    assert (legacy / "thread.jsonl").read_text(encoding="utf-8") == "first reply"


@pytest.mark.parametrize(
    ("field", "value"),
    [("tenant_id", "../tenant"), ("user_id", "user/name")],
)
def test_project_runtime_volume_rejects_unsafe_identity(tmp_path, field, value):
    values = {
        "tenant_id": "tenant",
        "user_id": "user",
        "project_scope_id": "chat",
    }
    values[field] = value
    provider = LocalPosixProjectRuntimeVolumeProvider(str(tmp_path))

    with pytest.raises(ValueError, match=field):
        provider.ensure(**values)


def test_failed_project_snapshot_preserves_private_projection_for_retry(tmp_path, monkeypatch):
    store = FilesystemObjectStore(root=str(tmp_path / "cipher"), materialized_root=str(tmp_path / "plain"))
    provider = EncryptedObjectStoreProjectRuntimeVolumeProvider(store)
    volume = provider.ensure(tenant_id="tenant", user_id="user", project_scope_id="project")
    marker = Path(volume.path, "thread.jsonl")
    marker.write_text("unsaved thread", encoding="utf-8")
    persist = store.persist_materialized_bytes

    def unavailable(*args, **kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(store, "persist_materialized_bytes", unavailable)
    with pytest.raises(OSError, match="storage unavailable"):
        provider.release(volume)
    assert marker.read_text(encoding="utf-8") == "unsaved thread"
    monkeypatch.setattr(store, "persist_materialized_bytes", persist)
    assert provider.release(volume) == 1
    restored = provider.ensure(tenant_id="tenant", user_id="user", project_scope_id="project")
    assert Path(restored.path, "thread.jsonl").read_text(encoding="utf-8") == "unsaved thread"


def test_encrypted_runtime_volume_rehydrates_sqlite_and_removes_plaintext(tmp_path):
    cipher_root = tmp_path / "cipher"
    materialized_root = tmp_path / "materialized"
    store = FilesystemObjectStore(
        root=str(cipher_root),
        materialized_root=str(materialized_root),
        master_key=b"K" * 32,
    )
    provider = EncryptedObjectStoreProjectRuntimeVolumeProvider(store)
    first = provider.ensure(
        tenant_id="tenant",
        user_id="user",
        project_scope_id="chat",
    )
    codex = Path(first.path, ".codex")
    codex.mkdir()
    Path(codex, "AGENTS.md").write_text("keep this", encoding="utf-8")
    with sqlite3.connect(Path(codex, "state.sqlite")) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('resumed')")
        connection.commit()

    assert provider.release(first) >= 2
    assert not Path(first.path).exists()
    keys = store.list_keys(f"{first.storage_prefix}/")
    assert keys
    for key in keys:
        durable = Path(store.root, *key.split("/")).read_bytes()
        assert durable.startswith(MAGIC)
        assert b"keep this" not in durable
        assert b"resumed" not in durable

    second = provider.ensure(
        tenant_id="tenant",
        user_id="user",
        project_scope_id="chat",
    )
    assert Path(second.path, ".codex", "AGENTS.md").read_text(
        encoding="utf-8"
    ) == "keep this"
    with sqlite3.connect(Path(second.path, ".codex", "state.sqlite")) as connection:
        assert connection.execute("SELECT value FROM state").fetchone() == (
            "resumed",
        )
    provider.release(second)


def test_runtime_checkpoint_preserves_open_log_inode_and_later_writes(tmp_path):
    store = FilesystemObjectStore(root=str(tmp_path / "cipher"), master_key=b"L" * 32)
    provider = EncryptedObjectStoreProjectRuntimeVolumeProvider(store)
    volume = provider.ensure(tenant_id="tenant", user_id="user", project_scope_id="chat")
    log = Path(volume.path, "thread.jsonl")
    with log.open("ab") as writer:
        writer.write(b'{"turn":1}\n')
        writer.flush()
        inode = os.fstat(writer.fileno()).st_ino
        provider.sync(volume)
        assert log.stat().st_ino == inode
        writer.write(b'{"turn":2}\n')
        writer.flush()
        provider.sync(volume)
        assert log.stat().st_ino == inode
        assert log.read_bytes() == b'{"turn":1}\n{"turn":2}\n'
    provider.release(volume)
    restored = provider.ensure(tenant_id="tenant", user_id="user", project_scope_id="chat")
    assert Path(restored.path, "thread.jsonl").read_bytes() == b'{"turn":1}\n{"turn":2}\n'
    provider.release(restored)


def test_runtime_checkpoint_preserves_open_sqlite_wal_and_locks(tmp_path):
    import fcntl

    store = FilesystemObjectStore(root=str(tmp_path / "cipher"), master_key=b"Q" * 32)
    provider = EncryptedObjectStoreProjectRuntimeVolumeProvider(store)
    volume = provider.ensure(tenant_id="tenant", user_id="user", project_scope_id="chat")
    database = Path(volume.path, "state.sqlite")
    lock = Path(volume.path, "thread.lock")
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE state (value TEXT)")
        connection.execute("INSERT INTO state VALUES ('first')")
        connection.commit()
        with lock.open("wb") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            tracked = [database, Path(str(database) + "-wal"), Path(str(database) + "-shm"), lock]
            inodes = [path.stat().st_ino for path in tracked]
            provider.sync(volume)
            assert [path.stat().st_ino for path in tracked] == inodes
            with lock.open("rb") as contender, pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            connection.execute("INSERT INTO state VALUES ('second')")
            connection.commit()
            provider.sync(volume)
            assert [path.stat().st_ino for path in tracked] == inodes
            with sqlite3.connect(database) as reader:
                assert reader.execute("SELECT value FROM state ORDER BY rowid").fetchall() == [("first",), ("second",)]
    finally:
        connection.close()
    provider.release(volume)
    restored = provider.ensure(tenant_id="tenant", user_id="user", project_scope_id="chat")
    with sqlite3.connect(Path(restored.path, "state.sqlite")) as reader:
        assert reader.execute("SELECT value FROM state ORDER BY rowid").fetchall() == [("first",), ("second",)]
    provider.release(restored)


def test_encrypted_runtime_volume_ignores_symlink_without_reading_target(tmp_path):
    store = FilesystemObjectStore(
        root=str(tmp_path / "cipher"),
        materialized_root=str(tmp_path / "materialized"),
        master_key=b"S" * 32,
    )
    provider = EncryptedObjectStoreProjectRuntimeVolumeProvider(store)
    volume = provider.ensure(
        tenant_id="tenant",
        user_id="user",
        project_scope_id="chat",
    )
    Path(volume.path, "escape").symlink_to(tmp_path / "outside")

    assert provider.sync(volume) == 0
    assert store.list_keys(f"{volume.storage_prefix}/") == []
    store.release_materialized_prefix(volume.storage_prefix or "", volume.path)


def test_encrypted_runtime_volume_excludes_staged_codex_account_auth(tmp_path):
    store = FilesystemObjectStore(
        root=str(tmp_path / "cipher"),
        materialized_root=str(tmp_path / "materialized"),
        master_key=b"A" * 32,
    )
    provider = EncryptedObjectStoreProjectRuntimeVolumeProvider(store)
    volume = provider.ensure(
        tenant_id="tenant",
        user_id="user",
        project_scope_id="chat",
    )
    auth = Path(volume.path, ".codex", "auth.json")
    auth.parent.mkdir(parents=True)
    auth.write_text('{"tokens":{"access_token":"secret"}}', encoding="utf-8")
    Path(volume.path, ".codex", "state.jsonl").write_text(
        "durable thread state",
        encoding="utf-8",
    )

    assert provider.sync(volume) == 1
    keys = store.list_keys(f"{volume.storage_prefix}/")
    assert keys == [f"{volume.storage_prefix}/.codex/state.jsonl"]
    store.release_materialized_prefix(volume.storage_prefix or "", volume.path)


def test_encrypted_runtime_volume_tolerates_file_removed_after_manifest(
    monkeypatch,
    tmp_path,
):
    store = FilesystemObjectStore(
        root=str(tmp_path / "cipher"),
        materialized_root=str(tmp_path / "materialized"),
        master_key=b"R" * 32,
    )
    provider = EncryptedObjectStoreProjectRuntimeVolumeProvider(store)
    volume = provider.ensure(
        tenant_id="tenant",
        user_id="user",
        project_scope_id="chat",
    )
    durable = Path(volume.path, "thread.jsonl")
    durable.write_text("keep", encoding="utf-8")
    transient = Path(volume.path, ".codex", "shell_snapshots", "turn.sh")
    transient.parent.mkdir(parents=True)
    transient.write_text("temporary", encoding="utf-8")
    regular_files = provider._regular_files

    def remove_transient_after_manifest(root: str) -> dict[str, str]:
        files = regular_files(root)
        transient.unlink()
        return files

    monkeypatch.setattr(provider, "_regular_files", remove_transient_after_manifest)

    assert provider.sync(volume) == 1
    keys = store.list_keys(f"{volume.storage_prefix}/")
    assert keys == [f"{volume.storage_prefix}/thread.jsonl"]
    store.release_materialized_prefix(volume.storage_prefix or "", volume.path)
