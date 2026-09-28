"""VFS store — schema + RLS isolation (T1) + store CRUD/concurrency/LRU (T2).

RLS tests run as vibecanvas_app (app_engine) with app.tenant_id set; FK-target
tenant/workflow/chat rows are seeded first. Mirrors test_sync_tenant.py.
"""
import uuid
import pytest
from sqlalchemy import text

from vibecanvas_api.storage.vfs_store import VfsRepo, VfsEntryMeta
from vibecanvas_api.services.file_revision import vfs_row_revision
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.chat_project_repo import ChatProjectRepo
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


async def _seed(app_engine, tenant, wf_id, chat_id, user):
    # tenants / users are auth tables (no RLS) — insertable without the GUC.
    async with app_engine.begin() as c:
        await c.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"), {"t": tenant})
        await c.execute(text("INSERT INTO users(user_id,tenant_id,email) VALUES (:u,:t,:e)"),
                        {"u": user, "t": tenant, "e": f"{user.hex[:6]}@example.com"})
    # workflows / workflow_versions / chats are RLS-scoped — run under the
    # tenant GUC so their tenant_id DEFAULT resolves to `tenant`.
    async with session_scope(tenant_id=str(tenant), user_id=str(user)) as session:
        project = await ChatProjectRepo(session, str(user)).create(name="VFS")
        await WorkflowRepo(session, str(user)).create_workflow(
            wf_id=wf_id,
            name="W",
        )
        await ChatRepo(session, str(user)).register_session(
            wf_id,
            project_id=project["project_id"],
            name="Chat",
            chat_id=chat_id,
        )


@pytest.mark.asyncio
async def test_vfs_artifact_rls_isolation(app_engine):
    ta, tb = uuid.uuid4(), uuid.uuid4()
    ua, ub = uuid.uuid4(), uuid.uuid4()
    await _seed(app_engine, ta, "wf_a", "chat_a", ua)
    await _seed(app_engine, tb, "wf_b", "chat_b", ub)
    async with app_engine.connect() as c:
        await c.execute(text("SELECT set_config('app.tenant_id',:t,false)"), {"t": str(ta)})
        await c.execute(text("INSERT INTO vfs_artifacts(scope_id,path,object_key,content_type) "
                             "VALUES ('wf_a','/data/x.json','artifacts/k','text/plain')"))
        await c.commit()
        row = (await c.execute(text("SELECT tenant_id FROM vfs_artifacts WHERE scope_id='wf_a'"))).one()
        assert str(row[0]) == str(ta)  # GUC default filled tenant_id
    async with app_engine.connect() as c:
        await c.execute(text("SELECT set_config('app.tenant_id',:t,false)"), {"t": str(tb)})
        rows = (await c.execute(text("SELECT tenant_id, path FROM vfs_artifacts"))).all()
        assert {str(row.tenant_id) for row in rows} == {str(tb)}
        assert {row.path for row in rows} == {"/data/.keep", "/logs/.keep", "/chats/.keep", "/chats/chat_b/.keep"}


@pytest.mark.asyncio
async def test_vfs_scratch_rls_isolation(app_engine):
    ta, tb = uuid.uuid4(), uuid.uuid4()
    ua, ub = uuid.uuid4(), uuid.uuid4()
    await _seed(app_engine, ta, "wf_sa", "chat_sa", ua)
    await _seed(app_engine, tb, "wf_sb", "chat_sb", ub)
    async with app_engine.connect() as c:
        await c.execute(text("SELECT set_config('app.tenant_id',:t,false)"), {"t": str(ta)})
        await c.execute(text("INSERT INTO vfs_scratch(scope_id,path,object_key,content_type) "
                             "VALUES ('wf_sa','/memory/plan.md','scratch/k','text/plain')"))
        await c.commit()
    async with app_engine.connect() as c:
        await c.execute(text("SELECT set_config('app.tenant_id',:t,false)"), {"t": str(tb)})
        rows = (await c.execute(text("SELECT tenant_id, path FROM vfs_scratch"))).all()
        assert [(str(row.tenant_id), row.path) for row in rows] == [(str(tb), "/memory/.keep")]


from vibecanvas_api.storage.vfs_store import PostgresVfsStore
from vibecanvas_api.storage.sync_session import current_sync_tenant_id


@pytest.mark.asyncio
async def test_write_artifact_read_roundtrip_real_csv(app_engine):
    import pathlib, asyncio
    t, u = uuid.uuid4(), uuid.uuid4()
    await _seed(app_engine, t, "wf_r", "chat_r", u)
    csv_text = (pathlib.Path(__file__).parent / "fixtures/vfs/sample.csv").read_text()
    store = PostgresVfsStore()
    tok = current_sync_tenant_id.set(str(t))
    try:
        path = await asyncio.to_thread(
            store.write_artifact, wf_id="wf_r", category="files",
            basename="sample", content=csv_text, content_type="text")
        assert path == "/files/sample_1.txt"
        entry = await asyncio.to_thread(
            store.read, wf_id="wf_r", path=path)
        assert entry.content == csv_text
        assert entry.size_bytes == len(csv_text.encode())
    finally:
        current_sync_tenant_id.reset(tok)


@pytest.mark.asyncio
async def test_write_artifact_concurrent_no_collision(app_engine):
    import asyncio
    t, u = uuid.uuid4(), uuid.uuid4()
    await _seed(app_engine, t, "wf_c", "chat_c", u)
    store = PostgresVfsStore()

    def _w(i):
        tok = current_sync_tenant_id.set(str(t))
        try:
            return store.write_artifact(wf_id="wf_c", category="data",
                                        basename="q", content=f'{{"i":{i}}}',
                                        content_type="json")
        finally:
            current_sync_tenant_id.reset(tok)

    paths = await asyncio.gather(asyncio.to_thread(_w, 1), asyncio.to_thread(_w, 2))
    assert len(set(paths)) == 2, f"collision: {paths}"
    assert set(paths) == {"/data/q_1.json", "/data/q_2.json"}


@pytest.mark.asyncio
async def test_write_scratch_and_ls(app_engine):
    import asyncio
    t, u = uuid.uuid4(), uuid.uuid4()
    await _seed(app_engine, t, "wf_l", "chat_l", u)
    store = PostgresVfsStore()
    tok = current_sync_tenant_id.set(str(t))
    try:
        await asyncio.to_thread(store.write_scratch, wf_id="wf_l",
                                path="/memory/plan.md", content="do X")
        await asyncio.to_thread(store.write_artifact, wf_id="wf_l", category="data",
                                basename="q", content="[]", content_type="json")
        items = await asyncio.to_thread(store.ls, wf_id="wf_l", prefix="/")
        paths = {it.path for it in items}
        assert "/memory/plan.md" in paths and "/data/q_1.json" in paths
    finally:
        current_sync_tenant_id.reset(tok)


@pytest.mark.asyncio
async def test_lru_evicts_oldest(pg_session):
    from vibecanvas_api.storage.workflow_repo import WorkflowRepo
    from vibecanvas_api.storage.vfs_store import VfsRepo
    # Pure-repo LRU check on the superuser pg_session (no RLS). The
    # workflows/vfs_artifacts tenant_id is NOT NULL with a GUC default, so
    # seed a tenant + user and set app.tenant_id first; creator_user_id is
    # a UUID column (the plan's "u" string fails asyncpg UUID coercion —
    # same drift the sibling test_ref_repo_pg.py currently hits).
    t, u = uuid.uuid4(), uuid.uuid4()
    await pg_session.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"),
                             {"t": t})
    await pg_session.execute(
        text("INSERT INTO users(user_id,tenant_id,email) VALUES (:u,:t,:e)"),
        {"u": u, "t": t, "e": f"{u.hex[:6]}@example.com"})
    await pg_session.execute(text("SELECT set_config('app.tenant_id',:t,false)"),
                             {"t": str(t)})
    wf = await WorkflowRepo(pg_session, str(u)).create_workflow(name="W")
    repo = VfsRepo(pg_session, object_store=InMemoryObjectStore(), max_entries_per_scope=3)
    for i in range(5):
        await repo.write_artifact(wf_id=wf["wf_id"], tenant=t.hex, category="data",
                                  basename="q", content=f"{i}", content_type="text",
                                  wf_version=None)
    remaining = await repo.ls(wf_id=wf["wf_id"], prefix="/data")
    assert len(remaining) == 3


@pytest.mark.asyncio
async def test_read_touch_false_does_not_bump_last_access(app_engine):
    t, u = uuid.uuid4(), uuid.uuid4()
    await _seed(app_engine, t, "wf_nt", "chat_nt", u)
    # Object-backed text spans sessions; use the process-singleton store so the
    # write-block bytes are visible to the read-block fetch_bytes.
    os_ = get_object_store()
    async with session_scope(tenant_id=str(t)) as s:
        repo = VfsRepo(s, object_store=os_)
        await repo.write_artifact(wf_id="wf_nt", tenant=t.hex, category="data", basename="x",
                                  content="hello", content_type="text/plain")
        await s.commit()
    async with session_scope(tenant_id=str(t)) as s:
        before = (await VfsRepo(s).ls_meta(wf_id="wf_nt", prefix="/"))[0].last_access
    async with session_scope(tenant_id=str(t)) as s:
        e = await VfsRepo(s, object_store=os_).read(wf_id="wf_nt",
                                  path="/data/x_1.txt", touch=False)
        await s.commit()
        assert e is not None and e.content == "hello"
    async with session_scope(tenant_id=str(t)) as s:
        after = (await VfsRepo(s).ls_meta(wf_id="wf_nt", prefix="/"))[0].last_access
    assert after == before


@pytest.mark.asyncio
async def test_ls_meta_excludes_content_and_returns_meta(app_engine):
    t, u = uuid.uuid4(), uuid.uuid4()
    await _seed(app_engine, t, "wf_lm", "chat_lm", u)
    async with session_scope(tenant_id=str(t)) as s:
        repo = VfsRepo(s, object_store=InMemoryObjectStore())
        await repo.write_artifact(wf_id="wf_lm", tenant=t.hex, category="data", basename="d",
                                  content="a,b\n1,2", content_type="table/csv",
                                  wf_version="v1.sv0", abstract="two rows")
        await repo.write_scratch(wf_id="wf_lm", tenant=t.hex, path="/memory/plan.md",
                                 content="notes", content_type="text/plain")
        await s.commit()
    async with session_scope(tenant_id=str(t)) as s:
        rows = await VfsRepo(s).ls_meta(wf_id="wf_lm", prefix="/")
        stored = (
            await s.execute(
                text(
                    "SELECT abstract, abstract_ciphertext, abstract_nonce, "
                    "abstract_key_id FROM vfs_artifacts "
                    "WHERE scope_id='wf_lm' AND path='/data/d_1.csv'"
                )
            )
        ).mappings().one()
    assert all(isinstance(r, VfsEntryMeta) for r in rows)
    art = next(r for r in rows if r.kind == "artifact")
    scr = next(r for r in rows if r.kind == "scratch")
    assert art.path == "/data/d_1.csv" and art.wf_version == "v1.sv0" and art.abstract == "two rows"
    assert scr.path == "/memory/plan.md" and scr.wf_version is None
    assert not hasattr(rows[0], "content")
    assert stored["abstract"] == ""
    assert stored["abstract_key_id"] is not None
    assert stored["abstract_nonce"]
    assert stored["abstract_ciphertext"]
    assert "two rows" not in stored["abstract_ciphertext"]


def test_vfs_models_have_object_key_column():
    from vibecanvas_api.storage.models import VfsArtifact, VfsScratch
    assert "object_key" in VfsArtifact.__table__.columns
    assert "object_key" in VfsScratch.__table__.columns
    assert VfsArtifact.__table__.columns["object_key"].nullable is True
    assert VfsScratch.__table__.columns["object_key"].nullable is True


def test_vfs_scratch_is_scope_scoped():
    from vibecanvas_api.storage.models import VfsScratch
    cols = VfsScratch.__table__.columns
    assert "scope_id" in cols
    assert "chat_id" not in cols
    assert cols["scope_id"].primary_key is True
    assert cols["path"].primary_key is True


# ---- binary read/write methods (T3) ----------------------------------------
# These run on the superuser pg_session (no RLS); the vfs FKs require real
# workflows/chats rows, so seed tenant+user+workflow+chat first (mirrors
# test_lru_evicts_oldest). The `tenant` arg passed to the binary writers is
# only used to build the ObjectStore key, so we feed the real tenant uuid hex.
from vibecanvas_api.services.object_store import InMemoryObjectStore, get_object_store
from vibecanvas_api.storage.models import VfsArtifact

PNG = b"\x89PNG\r\n\x1a\n\x00\xff\x10"   # non-UTF8 bytes — would corrupt in a text column


async def _seed_pg(pg_session):
    """Seed tenant/user/workflow/chat under the superuser session + GUC.
    Returns (tenant_hex, wf_id, chat_id)."""
    from vibecanvas_api.storage.workflow_repo import WorkflowRepo
    t, u = uuid.uuid4(), uuid.uuid4()
    await pg_session.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"),
                             {"t": t})
    await pg_session.execute(
        text("INSERT INTO users(user_id,tenant_id,email) VALUES (:u,:t,:e)"),
        {"u": u, "t": t, "e": f"{u.hex[:6]}@example.com"})
    await pg_session.execute(text("SELECT set_config('app.tenant_id',:t,false)"),
                             {"t": str(t)})
    wf = await WorkflowRepo(pg_session, str(u)).create_workflow(name="W")
    wf_id = wf["wf_id"]
    chat_id = uuid.uuid4().hex[:12]
    await pg_session.execute(text("SELECT set_config('app.user_id',:u,false)"), {"u": str(u)})
    project = await ChatProjectRepo(pg_session, str(u)).create(name="VFS")
    await ChatRepo(pg_session, str(u)).register_session(
        wf_id,
        project_id=project["project_id"],
        name="Chat",
        chat_id=chat_id,
    )
    await pg_session.flush()
    return t.hex, wf_id, chat_id


@pytest.mark.asyncio
async def test_write_artifact_bytes_roundtrip(pg_session):
    tenant, wf_id, _ = await _seed_pg(pg_session)
    os_ = InMemoryObjectStore()
    repo = VfsRepo(pg_session, object_store=os_)
    path = await repo.write_artifact_bytes(
        wf_id=wf_id, tenant=tenant, category="data", basename="img",
        data=PNG, content_type="image/png")
    assert path == "/data/img_1.png"
    got = await repo.read_bytes(wf_id=wf_id, path=path)
    assert got == PNG
    row = await pg_session.get(VfsArtifact, (wf_id, path))
    assert row.object_key == f"artifacts/{tenant}/{wf_id}/data/img_1.png"
    assert row.content_type == "image/png" and row.size_bytes == len(PNG)


@pytest.mark.asyncio
async def test_write_scratch_bytes_roundtrip_and_overwrite(pg_session):
    tenant, wf_id, _ = await _seed_pg(pg_session)
    os_ = InMemoryObjectStore()
    repo = VfsRepo(pg_session, object_store=os_)
    await repo.write_scratch_bytes(wf_id=wf_id, tenant=tenant, path="/memory/x.bin",
                                   data=b"\x00\x01", content_type="application/octet-stream")
    assert await repo.read_bytes(wf_id=wf_id, path="/memory/x.bin") == b"\x00\x01"
    await repo.write_scratch_bytes(wf_id=wf_id, tenant=tenant, path="/memory/x.bin",
                                   data=b"\x02\x03", content_type="application/octet-stream")
    assert await repo.read_bytes(wf_id=wf_id, path="/memory/x.bin") == b"\x02\x03"


@pytest.mark.asyncio
async def test_read_bytes_text_row_backcompat(pg_session):
    tenant, wf_id, _ = await _seed_pg(pg_session)
    repo = VfsRepo(pg_session, object_store=InMemoryObjectStore())
    await repo.write_artifact(wf_id=wf_id, tenant=tenant, category="files", basename="n",
                              content="hello", content_type="text/plain")
    got = await repo.read_bytes(wf_id=wf_id, path="/files/n_1.txt")
    assert got == b"hello"


# ---- sync facade binary methods (T4) ---------------------------------------
# Mirrors test_write_artifact_read_roundtrip_real_csv: seed via app_engine,
# set current_sync_tenant_id, drive the SYNC facade from the async test via
# asyncio.to_thread. The facade opens its OWN short NullPool session and reads
# the tenant from the CV; write_artifact_bytes takes NO `tenant` arg. The
# default test object store is the inmemory process singleton, so the facade's
# read_bytes (separate session, same singleton store) sees the written blob.
@pytest.mark.asyncio
async def test_facade_write_read_bytes(app_engine):
    import asyncio
    t, u = uuid.uuid4(), uuid.uuid4()
    await _seed(app_engine, t, "wf_fb", "chat_fb", u)
    store = PostgresVfsStore()
    tok = current_sync_tenant_id.set(str(t))
    try:
        path = await asyncio.to_thread(
            store.write_artifact_bytes, wf_id="wf_fb", category="data",
            basename="img", data=b"\x89PNG\x01\xff", content_type="image/png")
        assert path.endswith(".png")
        got = await asyncio.to_thread(
            store.read_bytes, wf_id="wf_fb", path=path)
        assert got == b"\x89PNG\x01\xff"
    finally:
        current_sync_tenant_id.reset(tok)


@pytest.mark.asyncio
async def test_facade_write_read_scratch_bytes(app_engine):
    import asyncio
    t, u = uuid.uuid4(), uuid.uuid4()
    await _seed(app_engine, t, "wf_fs", "chat_fs", u)
    store = PostgresVfsStore()
    tok = current_sync_tenant_id.set(str(t))
    try:
        await asyncio.to_thread(
            store.write_scratch_bytes, wf_id="wf_fs", path="/memory/b.bin",
            data=b"\x00\x01\x02", content_type="application/octet-stream")
        got = await asyncio.to_thread(
            store.read_bytes, wf_id="wf_fs", path="/memory/b.bin")
        assert got == b"\x00\x01\x02"
    finally:
        current_sync_tenant_id.reset(tok)


@pytest.mark.asyncio
async def test_evict_frees_binary_blob(pg_session):
    tenant, wf_id, _ = await _seed_pg(pg_session)
    os_ = InMemoryObjectStore()
    repo = VfsRepo(pg_session, object_store=os_, max_entries_per_scope=2)
    keys = []
    for i in range(3):
        p = await repo.write_artifact_bytes(wf_id=wf_id, tenant=tenant, category="data",
                                            basename="b", data=bytes([i]),
                                            content_type="application/octet-stream")
        keys.append(f"artifacts/{tenant}/{wf_id}{p}")
    with pytest.raises(KeyError):
        os_.fetch_bytes(keys[0])
    assert os_.fetch_bytes(keys[2]) == b"\x02"


@pytest.mark.asyncio
async def test_scratch_wf_scoped_roundtrip(pg_session):
    tenant, wf_id, _chat = await _seed_pg(pg_session)
    repo = VfsRepo(pg_session, object_store=InMemoryObjectStore())
    await repo.write_scratch(wf_id=wf_id, tenant=tenant, path="/memory/n", content="hi")
    got = await repo.read(wf_id=wf_id, path="/memory/n")          # no chat_id
    assert got is not None and got.content == "hi"
    rows = await repo.ls(wf_id=wf_id, prefix="/memory/")          # no chat_id
    assert any(r.path == "/memory/n" for r in rows)


# ---- VFS storage unification — text writers externalize to ObjectStore ------
# Every NEW text write now sets object_key + content="" (bytes in the store);
# read() fetch+decodes; legacy content-rows (object_key NULL) stay readable.
@pytest.mark.asyncio
async def test_text_write_externalizes_to_objectstore(pg_session):
    os_ = InMemoryObjectStore()
    tenant, wf_id, _ = await _seed_pg(pg_session)
    repo = VfsRepo(pg_session, object_store=os_)
    path = await repo.write_artifact(wf_id=wf_id, tenant=tenant, category="files",
                                     basename="n", content="hello", content_type="text/plain")
    row = await pg_session.get(VfsArtifact, (wf_id, path))
    assert row.object_key and row.size_bytes == 5   # bytes externalized
    assert os_.fetch_bytes(row.object_key) == b"hello"
    e = await repo.read(wf_id=wf_id, path=path)
    assert e.content == "hello"                       # fetch+decode roundtrip
    assert await repo.read_bytes(wf_id=wf_id, path=path) == b"hello"


@pytest.mark.asyncio
async def test_text_write_unknown_ct_keeps_txt_ext(pg_session):
    os_ = InMemoryObjectStore()
    tenant, wf_id, _ = await _seed_pg(pg_session)
    repo = VfsRepo(pg_session, object_store=os_)
    path = await repo.write_artifact(wf_id=wf_id, tenant=tenant, category="files",
                                     basename="m", content="x", content_type="some/unknown")
    assert path.endswith(".txt")                      # NOT .bin


@pytest.mark.asyncio
async def test_backfill_moves_legacy_content_row_to_objectstore(pg_session, monkeypatch):
    """Post-unification the `content` column is dropped, so a legacy row can no
    longer exist. This proves the one-shot backfill script: transiently re-add
    the column (mirrors migration 020.downgrade), seed a legacy row (content set,
    object_key NULL), run the backfill key/put/update logic, then drop the column
    again — the row is now object-backed at the bytes-writer key and reads back.
    """
    import importlib.util
    import pathlib

    # Load the standalone management script (api/scripts/ is not a package).
    _bf_path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "backfill_vfs_content.py"
    _spec = importlib.util.spec_from_file_location("backfill_vfs_content", _bf_path)
    _bf = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_bf)
    _backfill_table = _bf._backfill_table

    tenant, wf_id, _ = await _seed_pg(pg_session)
    os_ = InMemoryObjectStore()
    # The script does `from ...object_store import get_object_store` → patch the
    # name bound in the loaded script module.
    monkeypatch.setattr(_bf, "get_object_store", lambda: os_)

    conn = await pg_session.connection()
    # Transiently re-add the dropped column (migration 020.downgrade shape).
    await conn.execute(text(
        "ALTER TABLE vfs_artifacts ADD COLUMN IF NOT EXISTS content "
        "TEXT NOT NULL DEFAULT ''"))
    try:
        # Seed a pre-unification row: content set, object_key NULL.
        await conn.execute(text(
            "INSERT INTO vfs_artifacts(scope_id,path,content,content_type,size_bytes) "
            "VALUES (:w,'/files/old_1.txt','legacy','text/plain',6)"), {"w": wf_id})

        moved, found = await _backfill_table(conn, "vfs_artifacts", "artifacts")
        assert moved == 1 and found == 1
        # Re-running is idempotent — the row is now object-backed (skipped).
        assert (await _backfill_table(conn, "vfs_artifacts", "artifacts")) == (0, 0)

        # The key is built from the row's own tenant_id + wf_id + path (the
        # bytes-writer layout), and the blob is fetchable at exactly that key.
        row = (await conn.execute(text(
            "SELECT tenant_id, object_key, content FROM vfs_artifacts "
            "WHERE scope_id=:w AND path='/files/old_1.txt'"), {"w": wf_id})).one()
        row_tenant, object_key, content_col = row
        assert object_key == f"artifacts/{row_tenant}/{wf_id}/files/old_1.txt"
        assert content_col == ""
        assert os_.fetch_bytes(object_key) == b"legacy"
    finally:
        await conn.execute(text("ALTER TABLE vfs_artifacts DROP COLUMN IF EXISTS content"))

    # And the object-backed row reads back through the repo (no content column).
    pg_session.expire_all()
    repo = VfsRepo(pg_session, object_store=os_)
    e = await repo.read(wf_id=wf_id, path="/files/old_1.txt")
    assert e.content == "legacy"
    assert await repo.read_bytes(wf_id=wf_id, path="/files/old_1.txt") == b"legacy"


# ---- explicit-path durable ingress -----------------------------------------
# Explicit-path writers (upsert_artifact / upsert_artifact_bytes): the path is
# preserved verbatim (no `_N` seq), 2nd write at the same path overwrites in
# place (replaced=True, single object key, no orphan), and
# the artifact LRU, and `_validate_artifact_path` is the security boundary.
from vibecanvas_api.storage.vfs_store import _validate_artifact_path


def test_validate_artifact_path_accepts_mount_and_data():
    # Both /mount and /data are user-writable durable prefixes.
    assert _validate_artifact_path("/mount/sales.csv") == "/mount/sales.csv"
    # implicit folders (S3-style) are allowed
    assert _validate_artifact_path("/mount/sub/x.png") == "/mount/sub/x.png"
    assert _validate_artifact_path("/data/notes.csv") == "/data/notes.csv"
    assert _validate_artifact_path("/data/sub/y.jsonl") == "/data/sub/y.jsonl"


def test_validate_artifact_path_rejects_bad():
    import pytest as _pytest
    # Non-allowlisted prefixes (/memory, /etc, bare names) and traversal/control
    # chars stay rejected — the allowlist is the security boundary.
    for bad in ("/mount/../x", "/mount/../../etc/passwd", "/etc/passwd",
                "mount/x.csv", "/memory/x.md", "/logs/a.txt", "/skills/s.py",
                "/data/../x", "/data/", "/data/.", "/data/\x00x",
                "/mount/", "/mount/\x00x", "/mount/a/../b", "", "/mount/."):
        with _pytest.raises(ValueError):
            _validate_artifact_path(bad)


@pytest.mark.asyncio
async def test_upsert_artifact_explicit_path_preserved(pg_session):
    tenant, wf_id, _ = await _seed_pg(pg_session)
    repo = VfsRepo(pg_session, object_store=InMemoryObjectStore())
    replaced = await repo.upsert_artifact(
        wf_id=wf_id, tenant=tenant, path="/mount/sales.csv",
        content="a,b\n1,2", content_type="table/csv")
    assert replaced is False
    e = await repo.read(wf_id=wf_id, path="/mount/sales.csv")
    assert e is not None and e.content == "a,b\n1,2"
    row = await pg_session.get(VfsArtifact, (wf_id, "/mount/sales.csv"))
    assert row.path == "/mount/sales.csv"
    assert row.object_key == f"artifacts/{tenant}/{wf_id}/mount/sales.csv"


@pytest.mark.asyncio
async def test_upsert_artifact_overwrite_in_place_returns_replaced(pg_session):
    tenant, wf_id, _ = await _seed_pg(pg_session)
    os_ = InMemoryObjectStore()
    repo = VfsRepo(pg_session, object_store=os_)
    r1 = await repo.upsert_artifact(wf_id=wf_id, tenant=tenant,
                                    path="/mount/ref.txt", content="v1")
    assert r1 is False
    r2 = await repo.upsert_artifact(wf_id=wf_id, tenant=tenant,
                                    path="/mount/ref.txt", content="v2")
    assert r2 is True                          # second write REPLACED
    # exactly one row + one object key (no orphan)
    rows = await repo.ls(wf_id=wf_id, prefix="/mount/")
    assert len([x for x in rows if x.path == "/mount/ref.txt"]) == 1
    key = f"artifacts/{tenant}/{wf_id}/mount/ref.txt"
    assert os_.fetch_bytes(key) == b"v2"


@pytest.mark.asyncio
async def test_upsert_artifact_bytes_png_byte_identical(pg_session):
    tenant, wf_id, _ = await _seed_pg(pg_session)
    repo = VfsRepo(pg_session, object_store=InMemoryObjectStore())
    replaced = await repo.upsert_artifact_bytes(
        wf_id=wf_id, tenant=tenant, path="/mount/img.png",
        data=PNG, content_type="image/png")
    assert replaced is False
    assert await repo.read_bytes(wf_id=wf_id, path="/mount/img.png") == PNG


@pytest.mark.asyncio
async def test_upsert_artifact_bytes_same_content_keeps_revision(pg_session):
    tenant, wf_id, _ = await _seed_pg(pg_session)
    repo = VfsRepo(pg_session, object_store=InMemoryObjectStore())
    await repo.upsert_artifact_bytes(
        wf_id=wf_id,
        tenant=tenant,
        path="/data/diagrams/system.drawio",
        data=b'{"version":1}',
        content_type="application/vnd.jgraph.mxfile",
    )
    first = await pg_session.get(
        VfsArtifact,
        (wf_id, "/data/diagrams/system.drawio"),
    )
    first_revision = vfs_row_revision(first)

    replaced = await repo.upsert_artifact_bytes(
        wf_id=wf_id,
        tenant=tenant,
        path="/data/diagrams/system.drawio",
        data=b'{"version":1}',
        content_type="application/vnd.jgraph.mxfile",
    )
    pg_session.expire_all()
    second = await pg_session.get(
        VfsArtifact,
        (wf_id, "/data/diagrams/system.drawio"),
    )

    assert replaced is True
    assert vfs_row_revision(second) == first_revision


@pytest.mark.asyncio
async def test_compare_and_swap_artifact_rejects_stale_revision(pg_session):
    tenant, wf_id, _ = await _seed_pg(pg_session)
    repo = VfsRepo(pg_session, object_store=InMemoryObjectStore())
    created = await repo.compare_and_swap_artifact_bytes(
        wf_id=wf_id,
        tenant=tenant,
        path="/data/diagrams/system.drawio",
        expected_revision=None,
        data=b'{"version":1}',
        content_type="application/vnd.jgraph.mxfile",
    )
    assert created.committed is True
    assert created.revision

    stale = await repo.compare_and_swap_artifact_bytes(
        wf_id=wf_id,
        tenant=tenant,
        path="/data/diagrams/system.drawio",
        expected_revision=None,
        data=b'{"version":2}',
        content_type="application/vnd.jgraph.mxfile",
    )
    assert stale.committed is False
    assert stale.current_revision == created.revision
    assert await repo.read_bytes(
        wf_id=wf_id,
        path="/data/diagrams/system.drawio",
    ) == b'{"version":1}'

    replaced = await repo.compare_and_swap_artifact_bytes(
        wf_id=wf_id,
        tenant=tenant,
        path="/data/diagrams/system.drawio",
        expected_revision=created.revision,
        data=b'{"version":2}',
        content_type="application/vnd.jgraph.mxfile",
    )
    assert replaced.committed is True
    row = await pg_session.get(
        VfsArtifact,
        (wf_id, "/data/diagrams/system.drawio"),
    )
    assert replaced.revision == vfs_row_revision(row)
    assert await repo.read_bytes(
        wf_id=wf_id,
        path="/data/diagrams/system.drawio",
    ) == b'{"version":2}'


@pytest.mark.asyncio
async def test_mount_upsert_does_not_evict(pg_session):
    # Explicit user uploads do not invoke the agent artifact LRU.
    tenant, wf_id, _ = await _seed_pg(pg_session)
    repo = VfsRepo(pg_session, object_store=InMemoryObjectStore(),
                   max_entries_per_scope=1)
    await repo.upsert_artifact(wf_id=wf_id, tenant=tenant, path="/mount/a.txt", content="a")
    await repo.upsert_artifact(wf_id=wf_id, tenant=tenant, path="/mount/b.txt", content="b")
    rows = {r.path for r in await repo.ls(wf_id=wf_id, prefix="/mount/")}
    assert rows == {"/mount/a.txt", "/mount/b.txt"}


@pytest.mark.asyncio
async def test_facade_upsert_artifact(app_engine):
    import asyncio
    t, u = uuid.uuid4(), uuid.uuid4()
    await _seed(app_engine, t, "wf_up", "chat_up", u)
    store = PostgresVfsStore()
    tok = current_sync_tenant_id.set(str(t))
    try:
        replaced = await asyncio.to_thread(
            store.upsert_artifact, wf_id="wf_up", path="/mount/ref.txt",
            content="data", content_type="text/plain")
        assert replaced is False
        e = await asyncio.to_thread(store.read, wf_id="wf_up", path="/mount/ref.txt")
        assert e is not None and e.content == "data"
    finally:
        current_sync_tenant_id.reset(tok)
