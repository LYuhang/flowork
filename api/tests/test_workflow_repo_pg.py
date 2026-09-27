import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from vibecanvas_api.storage.models import Workflow, WorkflowVersion
from vibecanvas_api.storage.workflow_repo import WorkflowRepo

# WorkflowRepo stamps rows with the authenticated user id, which in production
# is a UUID string (AuthContext.user_id = str(sessions.user_id)) and is an
# FK to users.user_id. The `workflows`/`workflow_versions` tenant_id column
# resolves from the `app.tenant_id` GUC server-default. So a repo test must
# seed a tenant + the user rows and bind the session's tenant GUC.
TENANT = uuid.uuid4()
ALICE = uuid.uuid5(uuid.NAMESPACE_DNS, "alice")
BOB = uuid.uuid5(uuid.NAMESPACE_DNS, "bob")


async def _seed_and_bind(session):
    """Seed the tenant + alice/bob users (idempotent) and bind ``app.tenant_id``
    on this session so the tenant_id server-default resolves.

    pg_session/pg_engine connect as the RLS-bypassing superuser, so a plain
    INSERT works; the GUC is still required because tenant_id's DEFAULT reads
    ``current_setting('app.tenant_id')``.
    """
    await session.execute(
        text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'repo-test') "
             "ON CONFLICT (tenant_id) DO NOTHING"),
        {"t": TENANT},
    )
    for uid, email in ((ALICE, "alice@test"), (BOB, "bob@test")):
        await session.execute(
            text("INSERT INTO users(user_id, tenant_id, email) "
                 "VALUES (:u, :t, :e) ON CONFLICT (user_id) DO NOTHING"),
            {"u": uid, "t": TENANT, "e": email},
        )
    # Mirror db.py session_scope: set_config (NOT `SET`) accepts a bound param.
    # is_local=false → the GUC survives across this session's commits (the
    # concurrent test commits mid-transaction).
    await session.execute(
        text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(TENANT)}
    )


@pytest.mark.asyncio
async def test_major_creation_uses_selected_parent_not_global_head_and_upload_stays_on_selected_major(pg_session):
    await _seed_and_bind(pg_session)
    repo = WorkflowRepo(pg_session, str(ALICE))
    wf_id = (await repo.create_workflow(name="Branches", initial_workflow={"original": {}}))["wf_id"]
    await repo.commit(wf_id, {"one": {}}, stamp_metadata=True)
    await repo.new_version(wf_id, {"two": {}}, stamp_metadata=True)
    # Global HEAD is v2; this Chat selected v1.sv1.
    source = await repo.get_workflow_at(wf_id, 1, 1)
    await repo.get_meta(wf_id, for_update=True)
    major = await repo.new_version(wf_id, source, stamp_metadata=True, source_version=(1, 1))
    assert major == 3
    row = await pg_session.get(WorkflowVersion, (wf_id, 3, 0))
    assert (row.parent_major, row.parent_sub) == (1, 1)
    graph = await repo.get_workflow_at(wf_id, 3, 0)
    assert "one" in graph and "two" not in graph
    assert graph["__meta__"]["workflow_version"] == 3
    assert source["__meta__"]["workflow_version"] == 1
    pointer = await repo.commit(wf_id, {"edited": {}}, target_major=1, stamp_metadata=True)
    assert (pointer.parent_v, pointer.sv) == (1, 2)
    assert "two" in await repo.get_workflow_at(wf_id, 2, 0)
    assert "one" in await repo.get_workflow_at(wf_id, 3, 0)


@pytest.mark.asyncio
async def test_create_and_get(pg_session):
    await _seed_and_bind(pg_session)
    repo = WorkflowRepo(pg_session, user_id=str(ALICE))
    meta = await repo.create_workflow(name="My WF")
    wf_id = meta["wf_id"]
    assert meta["workflow_name"] == "My WF"
    got = await repo.get_meta(wf_id)
    assert got["wf_id"] == wf_id


@pytest.mark.asyncio
async def test_metadata_patch_preserves_omitted_fields_and_workflow_version(pg_session):
    await _seed_and_bind(pg_session)
    repo = WorkflowRepo(pg_session, str(ALICE))
    meta = await repo.create_workflow(name="Original", description="Keep", tags=["old"], initial_workflow={"node": {}})
    wf_id = meta["wf_id"]
    updated = await repo.update_meta(wf_id, workflow_name="Renamed")
    assert updated["workflow_name"] == "Renamed"
    assert updated["description"] == "Keep" and updated["tags"] == ["old"]
    updated = await repo.update_meta(wf_id, description="", tags=[])
    assert updated["workflow_name"] == "Renamed"
    assert updated["description"] == "" and updated["tags"] == []
    assert (updated["active_major"], updated["active_sub"]) == (1, 0)
    assert await repo.get_current_workflow(wf_id) == {"node": {}}


@pytest.mark.asyncio
async def test_upload_commit_stamps_allocated_version_and_preserves_old_graph(pg_session):
    await _seed_and_bind(pg_session)
    repo = WorkflowRepo(pg_session, str(ALICE))
    meta = await repo.create_workflow(name="Keep name", description="Keep description", tags=["keep"], initial_workflow={"old": {}})
    wf_id = meta["wf_id"]
    source = {"__meta__": {"workflow_name": "Ignored", "workflow_version": 99, "workflow_subversion": 99}, "new": {}}
    first = await repo.commit(wf_id, source, stamp_metadata=True)
    second = await repo.commit(wf_id, source, stamp_metadata=True)
    assert (first.sv, second.sv) == (1, 2)
    assert await repo.get_workflow_at(wf_id, 1, 0) == {"old": {}}
    graph = await repo.get_workflow_at(wf_id, 1, 2)
    assert "old" not in graph and "new" in graph
    assert graph["__meta__"] == {"workflow_id": wf_id, "workflow_name": "Keep name", "workflow_version": 1, "workflow_subversion": 2}
    assert source["__meta__"]["workflow_subversion"] == 99
    current = await repo.get_meta(wf_id)
    assert current["description"] == "Keep description" and current["tags"] == ["keep"]


@pytest.mark.asyncio
async def test_parallel_upload_commits_allocate_distinct_correctly_stamped_versions(pg_session, pg_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    await _seed_and_bind(pg_session)
    wf_id = (await WorkflowRepo(pg_session, str(ALICE)).create_workflow(name="Concurrent"))["wf_id"]
    await pg_session.commit()
    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    ready = asyncio.Queue()
    start = asyncio.Event()
    async def upload(label):
        async with maker() as session:
            await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(TENANT)})
            stale = await session.get(Workflow, wf_id)
            ready.put_nowait(True)
            await start.wait()
            pointer = await WorkflowRepo(session, str(ALICE)).commit(wf_id, {label: {}}, stamp_metadata=True)
            assert stale is not None
            await session.commit()
            return pointer.sv, label
    tasks = [asyncio.create_task(upload(label)) for label in ("one", "two")]
    try:
        await asyncio.wait_for(ready.get(), timeout=10)
        await asyncio.wait_for(ready.get(), timeout=10)
        start.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert {sub for sub, _ in results} == {1, 2}
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(TENANT)})
        repo = WorkflowRepo(session, str(ALICE))
        for sub, label in results:
            graph = await repo.get_workflow_at(wf_id, 1, sub)
            assert label in graph and graph["__meta__"]["workflow_subversion"] == sub
        assert (await repo.get_meta(wf_id))["active_sub"] == 2


@pytest.mark.asyncio
async def test_parallel_metadata_patches_merge_under_row_lock(pg_session, pg_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    await _seed_and_bind(pg_session)
    repo = WorkflowRepo(pg_session, str(ALICE))
    wf_id = (await repo.create_workflow(name="Original", description="Original", tags=["keep"]))["wf_id"]
    await pg_session.commit()
    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    ready = asyncio.Queue()
    start = asyncio.Event()

    async def patch(fields):
        async with maker() as session:
            await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(TENANT)})
            # Keep a stale ORM instance alive to exercise populate_existing
            # as well as database read/merge/write serialization.
            loaded = await session.get(Workflow, wf_id)
            ready.put_nowait(True)
            await start.wait()
            await WorkflowRepo(session, str(ALICE)).update_meta(wf_id, **fields)
            assert loaded is not None
            await session.commit()

    tasks = [asyncio.create_task(patch(fields)) for fields in ({"workflow_name": "Renamed"}, {"description": "New description"})]
    try:
        await asyncio.wait_for(ready.get(), timeout=10)
        await asyncio.wait_for(ready.get(), timeout=10)
        start.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    async with maker() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(TENANT)})
        result = await WorkflowRepo(session, str(ALICE)).get_meta(wf_id)
    assert result["workflow_name"] == "Renamed"
    assert result["description"] == "New description"
    assert result["tags"] == ["keep"]
    assert (result["active_major"], result["active_sub"]) == (1, 0)


@pytest.mark.asyncio
async def test_commit_bumps_sub_and_moves_head(pg_session):
    await _seed_and_bind(pg_session)
    repo = WorkflowRepo(pg_session, user_id=str(ALICE))
    wf_id = (await repo.create_workflow(name="W"))["wf_id"]
    await repo.commit(wf_id, {"node_1": {"node_type": "StartNode"}}, note="c1")
    await repo.commit(wf_id, {"node_1": {"node_type": "StartNode"}, "node_2": {}}, note="c2")
    meta = await repo.get_meta(wf_id)
    assert (meta["active_major"], meta["active_sub"]) == (1, 2)
    wf = await repo.get_current_workflow(wf_id)
    assert "node_2" in wf


@pytest.mark.asyncio
async def test_workflow_versions_are_ciphertext_only(pg_session):
    await _seed_and_bind(pg_session)
    repo = WorkflowRepo(pg_session, user_id=str(ALICE))
    wf_id = (await repo.create_workflow(name="Encrypted"))["wf_id"]
    workflow = {"node_1": {"node_type": "StartNode", "private": "value"}}
    pointer = await repo.commit(wf_id, workflow, note="encrypted")

    row = (
        await pg_session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.wf_id == wf_id,
                WorkflowVersion.major == 1,
                WorkflowVersion.sub == pointer.sv,
            )
        )
    ).scalar_one()
    assert row.workflow_ciphertext
    assert row.workflow_nonce
    assert row.workflow_key_id is not None
    assert await repo.get_current_workflow(wf_id) == workflow


@pytest.mark.asyncio
async def test_workflow_display_metadata_and_version_notes_are_ciphertext_only(
    pg_session,
):
    await _seed_and_bind(pg_session)
    repo = WorkflowRepo(pg_session, user_id=str(ALICE))
    title = "private-workflow-title-sentinel"
    note = "private-version-note-sentinel"
    wf_id = (
        await repo.create_workflow(
            name=title,
            description="private workflow description",
            tags=["private-tag"],
        )
    )["wf_id"]
    pointer = await repo.commit(wf_id, {"node": {}}, note=note)

    workflow_row = await pg_session.get(Workflow, wf_id)
    version_row = await pg_session.get(
        WorkflowVersion,
        {"wf_id": wf_id, "major": 1, "sub": pointer.sv},
    )
    assert workflow_row is not None and workflow_row.metadata_ciphertext
    assert title not in workflow_row.metadata_ciphertext
    assert version_row is not None and version_row.note_ciphertext
    assert note not in version_row.note_ciphertext

    columns = set((await pg_session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name='workflows'"
    ))).scalars())
    assert {"workflow_name", "description", "tags"}.isdisjoint(columns)
    version_columns = set((await pg_session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name='workflow_versions'"
    ))).scalars())
    assert "note" not in version_columns

    assert (await repo.get_meta(wf_id))["workflow_name"] == title
    history = await repo.get_version_history(wf_id)
    assert next(item for item in history if item["sub"] == pointer.sv)["note"] == note


@pytest.mark.asyncio
async def test_concurrent_commits_atomic_sv(pg_session, pg_engine):
    """Two concurrent commits must not collide on (wf_id, major, sub)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    await _seed_and_bind(pg_session)
    repo = WorkflowRepo(pg_session, user_id=str(BOB))
    wf_id = (await repo.create_workflow(name="C"))["wf_id"]
    await pg_session.commit()
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)

    async def do_commit(i):
        async with sm() as s:
            await s.execute(
                text("SELECT set_config('app.tenant_id', :t, false)"),
                {"t": str(TENANT)},
            )
            r = WorkflowRepo(s, user_id=str(BOB))
            await r.commit(wf_id, {"k": i}, note=f"c{i}")
            await s.commit()

    await asyncio.gather(*[do_commit(i) for i in range(5)])
    async with sm() as s:
        await s.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"),
            {"t": str(TENANT)},
        )
        r = WorkflowRepo(s, user_id=str(BOB))
        history = await r.get_version_history(wf_id)
    subs = sorted(h["sub"] for h in history if h["major"] == 1)
    assert subs == [0, 1, 2, 3, 4, 5]  # sv0 from create + 5 commits, no dupes
