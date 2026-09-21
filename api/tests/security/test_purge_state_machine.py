from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.authorization.openfga_client import OpenFgaReadPage, OpenFgaTuple
from vibecanvas_api.security import purge


def test_every_purge_phase_has_a_real_handler():
    assert set(purge._PHASE_HANDLERS) == set(purge.PHASES)
    assert all(callable(purge._PHASE_HANDLERS[name]) for name in purge.PHASES)


def test_openfga_erasure_object_types_match_checked_in_model():
    model_path = (
        Path(__file__).resolve().parents[2]
        / "src/vibecanvas_api/authorization/model/model.json"
    )
    model = json.loads(model_path.read_text(encoding="utf-8"))
    object_types = {
        definition["type"]
        for definition in model["type_definitions"]
        if definition["type"] != "user"
    }

    assert purge._OPENFGA_USER_OBJECT_TYPES == object_types


@pytest.mark.asyncio
async def test_openfga_user_erasure_reads_each_typed_object_prefix():
    user_id = uuid.uuid4()

    class StrictOpenFgaClient:
        def __init__(self):
            self.keys: list[OpenFgaTuple] = []

        async def read(self, *, tuple_key, **_kwargs):
            assert tuple_key.user == f"user:{user_id}"
            assert tuple_key.object.endswith(":")
            assert tuple_key.object != ""
            self.keys.append(tuple_key)
            return OpenFgaReadPage(tuples=())

    client = StrictOpenFgaClient()
    found = await purge._read_user_openfga_tuples(client, user_id=user_id)

    assert found == set()
    assert {key.object[:-1] for key in client.keys} == (
        purge._OPENFGA_USER_OBJECT_TYPES
    )


@pytest.mark.asyncio
async def test_openfga_change_history_uses_scoped_erasure_function(monkeypatch):
    calls: list[tuple[object, ...]] = []

    class Connection:
        async def fetchval(self, *args):
            calls.append(args)
            return 7

        async def close(self):
            calls.append(("close",))

    async def connect(dsn):
        calls.append(("connect", dsn))
        return Connection()

    monkeypatch.setattr(purge, "_connect_openfga_erasure_database", connect)
    monkeypatch.setattr(
        purge.config,
        "openfga_erasure_database_url",
        "postgresql+asyncpg://erasure:secret@openfga/openfga",
    )
    monkeypatch.setattr(purge.config, "openfga_store_id", "store-1")

    removed = await purge._purge_openfga_change_history(
        subjects={"user:2", "user:1"},
        object_ids={"object-2", "object-1"},
    )

    assert removed == 7
    assert calls == [
        ("connect", "postgresql://erasure:secret@openfga/openfga"),
        (
            "SELECT public.flowork_erase_changelog($1, $2::text[], $3::text[])",
            "store-1",
            ["user:1", "user:2"],
            ["object-1", "object-2"],
        ),
        ("close",),
    ]


@pytest.mark.asyncio
async def test_runtime_erasure_constructs_manager_in_worker_process(
    pg_engine,
    monkeypatch,
):
    """Deletion must contact sandboxd even without an app-lifespan singleton."""

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with pg_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (tenant_id, name) VALUES (:t, 'runtime-purge')"),
            {"t": tenant_id},
        )

    calls: list[tuple[str, object]] = []

    class Manager:
        async def close_user(self, value, *, reason):
            calls.append(("close_user", (value, reason)))

        async def close_tenant(self, value, *, reason):
            calls.append(("close_tenant", (value, reason)))

        async def purge_user_storage(self, value, tenant_ids, personal_tenant_id):
            calls.append(
                (
                    "purge_user_storage",
                    (value, tenant_ids, personal_tenant_id),
                )
            )

    class HostMountBridge:
        async def unregister_user(self, *, user_id):
            calls.append(("unregister_user", user_id))

    manager = Manager()
    factory_calls = 0

    def manager_factory():
        nonlocal factory_calls
        factory_calls += 1
        return manager

    monkeypatch.setattr(purge, "get_sandbox_manager", manager_factory)
    monkeypatch.setattr(purge, "host_mount_bridge", HostMountBridge())
    monkeypatch.setattr(
        purge,
        "_user_tenant_ids",
        lambda _lease: _async_value((tenant_id,)),
    )
    monkeypatch.setattr(
        purge,
        "_chat_runtime_coordinates",
        lambda _lease: _async_value(()),
    )
    monkeypatch.setattr(purge, "_safe_remove_user_directory", lambda *_args: None)
    monkeypatch.setattr(purge, "_safe_remove_tenant_directory", lambda *_args: None)
    monkeypatch.setattr(purge, "_safe_remove_host_mount", lambda *_args: None)

    from vibecanvas_api.storage import db as db_mod

    old = db_mod._admin_engine
    db_mod._admin_engine = pg_engine
    try:
        await purge._purge_runtime_state(
            purge.PurgeLease(
                job_id=uuid.uuid4(),
                deletion_request_id=uuid.uuid4(),
                user_id=user_id,
                tenant_id=tenant_id,
                completed_phases=(),
            )
        )
    finally:
        db_mod._admin_engine = old

    assert factory_calls == 1
    assert ("close_user", (str(user_id), "account_purge")) in calls
    assert ("close_tenant", (str(tenant_id), "account_purge")) in calls
    assert (
        "purge_user_storage",
        (str(user_id), [str(tenant_id)], str(tenant_id)),
    ) in calls


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_database_erasure_breaks_published_skill_revision_cycle(pg_engine):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    async with pg_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (tenant_id, name) VALUES (:t, 'purge-skill')"),
            {"t": tenant_id},
        )
        await conn.execute(
            text(
                "INSERT INTO users "
                "(user_id, tenant_id, email, display_name, status) "
                "VALUES (:u, :t, :e, '', 'pending_deletion')"
            ),
            {"u": user_id, "t": tenant_id, "e": f"{user_id}@x.test"},
        )
        await conn.execute(
            text(
                "INSERT INTO skills "
                "(skill_id, tenant_id, user_id, name, description) "
                "VALUES (:s, :t, :u, 'erasable', '')"
            ),
            {"s": skill_id, "t": tenant_id, "u": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO skill_revisions "
                "(revision_id, skill_id, tenant_id, user_id, revision_hash, version) "
                "VALUES (:r, :s, :t, :u, 'revision-hash', 1)"
            ),
            {"r": revision_id, "s": skill_id, "t": tenant_id, "u": user_id},
        )
        await conn.execute(
            text(
                "UPDATE skills SET current_revision_id=:r "
                "WHERE skill_id=:s"
            ),
            {"r": revision_id, "s": skill_id},
        )

    from vibecanvas_api.storage import db as db_mod

    old = db_mod._admin_engine
    db_mod._admin_engine = pg_engine
    try:
        await purge._purge_database(
            purge.PurgeLease(
                job_id=uuid.uuid4(),
                deletion_request_id=uuid.uuid4(),
                user_id=user_id,
                tenant_id=tenant_id,
                completed_phases=(),
            )
        )
    finally:
        db_mod._admin_engine = old

    async with pg_engine.connect() as conn:
        counts = (
            await conn.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM skills WHERE tenant_id=:t), "
                    "(SELECT count(*) FROM skill_revisions WHERE tenant_id=:t)"
                ),
                {"t": tenant_id},
            )
        ).one()
    assert tuple(counts) == (0, 0)


@pytest.mark.asyncio
async def test_purge_job_completes_only_after_all_phases(pg_engine, monkeypatch):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    request_id = uuid.uuid4()
    job_id = uuid.uuid4()
    async with pg_engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO tenants (tenant_id, name) VALUES (:t, 'purge')"
        ), {"t": tenant_id})
        await conn.execute(text(
            "INSERT INTO users (user_id, tenant_id, email, display_name, status) "
            "VALUES (:u, :t, :e, '', 'pending_deletion')"
        ), {"u": user_id, "t": tenant_id, "e": f"{user_id}@x.test"})
        await conn.execute(text(
            "INSERT INTO account_deletion_requests "
            "(id, user_id, tenant_id, email_snapshot, status, purge_after) "
            "VALUES (:r, :u, :t, 'x@x.test', 'pending', now())"
        ), {"r": request_id, "u": user_id, "t": tenant_id})
        await conn.execute(text(
            "INSERT INTO data_purge_jobs "
            "(job_id, deletion_request_id, user_id, tenant_id, status, available_at) "
            "VALUES (:j, :r, :u, :t, 'queued', now())"
        ), {"j": job_id, "r": request_id, "u": user_id, "t": tenant_id})

    from vibecanvas_api.storage import db as db_mod
    old = db_mod._admin_engine
    db_mod._admin_engine = pg_engine
    called: list[str] = []

    def handler(name):
        async def _run(_lease):
            called.append(name)
        return _run

    monkeypatch.setattr(
        purge,
        "_PHASE_HANDLERS",
        {name: handler(name) for name in purge.PHASES},
    )
    try:
        lease = await purge.claim_due_purge_job()
        assert lease is not None
        await purge.run_purge_job(lease)
    finally:
        db_mod._admin_engine = old

    async with pg_engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT "
            "(SELECT count(*) FROM users WHERE user_id=:u) AS users, "
            "(SELECT count(*) FROM tenants WHERE tenant_id=:t) AS tenants, "
            "(SELECT count(*) FROM data_purge_jobs WHERE job_id=:j) AS jobs, "
            "(SELECT count(*) FROM account_deletion_requests WHERE id=:r) "
            "AS requests"
        ), {"u": user_id, "t": tenant_id, "j": job_id, "r": request_id})).one()
    assert called == list(purge.PHASES)
    assert row.users == 0
    assert row.tenants == 0
    assert row.jobs == 0
    assert row.requests == 0


@pytest.mark.asyncio
async def test_hard_delete_preserves_organization_content_with_anonymous_actor(
    pg_engine,
    monkeypatch,
):
    personal_tenant_id = uuid.uuid4()
    business_tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    request_id = uuid.uuid4()
    job_id = uuid.uuid4()
    group_id = uuid.uuid4()
    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (tenant_id, name) VALUES "
                "(:personal, 'personal'), (:business, 'business')"
            ),
            {"personal": personal_tenant_id, "business": business_tenant_id},
        )
        await conn.execute(
            text(
                "INSERT INTO users "
                "(user_id, tenant_id, email, display_name, status) "
                "VALUES (:u, :personal, :email, '', 'pending_deletion')"
            ),
            {
                "u": user_id,
                "personal": personal_tenant_id,
                "email": f"{user_id}@x.test",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO organizations "
                "(tenant_id, kind, slug, name, created_by) "
                "VALUES (:business, 'business', :slug, 'Shared', :u)"
            ),
            {"business": business_tenant_id, "slug": uuid.uuid4().hex, "u": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO groups (group_id, tenant_id, name, created_by) "
                "VALUES (:group_id, :business, 'Shared group', :u)"
            ),
            {"group_id": group_id, "business": business_tenant_id, "u": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO account_deletion_requests "
                "(id, user_id, tenant_id, email_snapshot, status, purge_after) "
                "VALUES (:r, :u, :personal, '', 'pending', now())"
            ),
            {"r": request_id, "u": user_id, "personal": personal_tenant_id},
        )
        await conn.execute(
            text(
                "INSERT INTO data_purge_jobs "
                "(job_id, deletion_request_id, user_id, tenant_id, status, "
                "available_at) VALUES "
                "(:j, :r, :u, :personal, 'queued', now())"
            ),
            {
                "j": job_id,
                "r": request_id,
                "u": user_id,
                "personal": personal_tenant_id,
            },
        )

    from vibecanvas_api.storage import db as db_mod

    old = db_mod._admin_engine
    db_mod._admin_engine = pg_engine

    async def completed_phase(_lease):
        return None

    monkeypatch.setattr(
        purge,
        "_PHASE_HANDLERS",
        {name: completed_phase for name in purge.PHASES},
    )
    try:
        lease = await purge.claim_due_purge_job()
        assert lease is not None
        await purge.run_purge_job(lease)
    finally:
        db_mod._admin_engine = old

    async with pg_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT group_row.created_by, actor.status, "
                    "actor.profile_ciphertext, actor.profile_key_id, "
                    "organization.created_by AS organization_actor, "
                    "(SELECT count(*) FROM users WHERE user_id=:u) AS erased_users, "
                    "(SELECT count(*) FROM tenants WHERE tenant_id=:personal) "
                    "AS erased_tenants "
                    "FROM groups AS group_row "
                    "JOIN users AS actor ON actor.user_id=group_row.created_by "
                    "JOIN organizations AS organization "
                    "  ON organization.tenant_id=group_row.tenant_id "
                    "WHERE group_row.group_id=:group_id"
                ),
                {
                    "u": user_id,
                    "personal": personal_tenant_id,
                    "group_id": group_id,
                },
            )
        ).one()
    assert row.created_by != user_id
    assert row.organization_actor == row.created_by
    assert row.status == "disabled"
    assert row.profile_ciphertext is None
    assert row.profile_key_id is None
    assert row.erased_users == 0
    assert row.erased_tenants == 0


@pytest.mark.asyncio
async def test_failed_purge_is_not_automatically_reclaimed(pg_engine, monkeypatch):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    request_id = uuid.uuid4()
    job_id = uuid.uuid4()
    async with pg_engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO tenants (tenant_id, name) VALUES (:t, 'purge-fail')"
        ), {"t": tenant_id})
        await conn.execute(text(
            "INSERT INTO users (user_id, tenant_id, email, display_name, status) "
            "VALUES (:u, :t, :e, '', 'pending_deletion')"
        ), {"u": user_id, "t": tenant_id, "e": f"{user_id}@x.test"})
        await conn.execute(text(
            "INSERT INTO account_deletion_requests "
            "(id, user_id, tenant_id, email_snapshot, status, purge_after) "
            "VALUES (:r, :u, :t, 'f@x.test', 'pending', now())"
        ), {"r": request_id, "u": user_id, "t": tenant_id})
        await conn.execute(text(
            "INSERT INTO data_purge_jobs "
            "(job_id, deletion_request_id, user_id, tenant_id, status, available_at) "
            "VALUES (:j, :r, :u, :t, 'queued', now())"
        ), {"j": job_id, "r": request_id, "u": user_id, "t": tenant_id})

    from vibecanvas_api.storage import db as db_mod
    old = db_mod._admin_engine
    db_mod._admin_engine = pg_engine

    async def fail(_lease):
        raise RuntimeError("password=must-not-persist")

    monkeypatch.setattr(
        purge,
        "_PHASE_HANDLERS",
        {**purge._PHASE_HANDLERS, purge.PHASES[0]: fail},
    )
    try:
        lease = await purge.claim_due_purge_job()
        assert lease is not None
        with pytest.raises(RuntimeError):
            await purge.run_purge_job(lease)
        assert await purge.claim_due_purge_job() is None
    finally:
        db_mod._admin_engine = old

    async with pg_engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT status, last_error_message FROM data_purge_jobs WHERE job_id = :j"
        ), {"j": job_id})).one()
    assert row.status == "failed"
    assert "must-not-persist" not in row.last_error_message
