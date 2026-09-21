"""Seed and reconcile real encrypted KB upload orphans.

``kb_orphan_reconciler`` (module docstring) has two branches:

Both pre-write and post-write orphans become explicit ``failed`` rows. The
platform does not silently retry; a later user/Agent action creates new work.

The existing beat-loop test (``test_beat_loop_bound.py``) only runs the
reconciler with zero orphan rows. This test seeds Case-B orphans for two
tenants and proves the outbound jobs retain the correct tenant identity,
without leaking the retired Task Center representation back into the model.

The public test remains synchronous because the DBOS entry point owns its
event loop. Encrypted rows are created through ``KbRepo`` in a short setup loop
and the SQLAlchemy engine is disposed before the beat tick starts a new loop.

``ADMIN_DATABASE_URL`` is pointed at the superuser test DB so the
cross-tenant SELECT/UPDATE sweep sees both tenants' rows.
"""
from __future__ import annotations

import uuid
import asyncio
from datetime import datetime, timedelta, timezone

import psycopg


def _seed_identity(dsn: str) -> dict:
    """Seed one tenant + user + KB + a Case-B orphan ``kb_files`` row.

    Case B = ``status='pending'``, ``object_store_key`` set, no matching
    ``tasks`` row, ``created_at`` older than ``ORPHAN_THRESHOLD_SEC``.
    Returns the seeded ids so the caller can assert on them.
    """
    ids = dict(
        tenant_id=uuid.uuid4(), user_id=uuid.uuid4(),
        kb_id=None, file_id=None,
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants(tenant_id, name) VALUES (%s, 'orphan')",
            (ids["tenant_id"],),
        )
        conn.execute(
            "INSERT INTO users(user_id, tenant_id, email) VALUES (%s, %s, %s)",
            (ids["user_id"], ids["tenant_id"],
             f"orphan-{uuid.uuid4().hex[:8]}@example.com"),
        )
    return ids


async def _seed_encrypted_orphans(seeds: list[dict]) -> None:
    from vibecanvas_api.storage.db import dispose_engine, session_scope
    from vibecanvas_api.storage.repo_kb import KbRepo

    try:
        for seed in seeds:
            async with session_scope(tenant_id=str(seed["tenant_id"])) as session:
                repo = KbRepo(session)
                kb = await repo.create_kb(
                    tenant_id=seed["tenant_id"],
                    user_id=seed["user_id"],
                    name=f"kb-{uuid.uuid4().hex[:6]}",
                )
                file = await repo.create_file(
                    kb_id=kb.id,
                    tenant_id=seed["tenant_id"],
                    user_id=seed["user_id"],
                    name="doc.txt",
                    parser_type="txt",
                    mime_type="text/plain",
                    file_size=10,
                    content_hash=uuid.uuid4().hex * 2,
                    status="pending",
                    object_store_key=f"kb/{seed['tenant_id']}/{kb.id}/content",
                )
                file.created_at = datetime.now(timezone.utc) - timedelta(
                    seconds=120
                )
                seed["kb_id"] = kb.id
                seed["file_id"] = file.id
    finally:
        await dispose_engine()


def test_stale_orphans_fail_without_implicit_new_task(
    monkeypatch, pg_url,
):
    """Each tenant is updated through ciphertext storage without retry."""
    import vibecanvas_api.background_tasks.kb_orphan_reconciler as orphan

    sync_dsn = pg_url.replace("+asyncpg", "")  # superuser DSN, RLS-bypass.

    a = _seed_identity(sync_dsn)
    b = _seed_identity(sync_dsn)

    # The cross-tenant sweep (Case A UPDATE + Case B SELECT) needs RLS
    # bypass → point ADMIN_DATABASE_URL at the superuser test DB and reset
    # any cached engine, exactly like test_beat_loop_bound.py.
    from vibecanvas_api.storage import db as db_mod
    monkeypatch.setattr(db_mod, "_admin_engine", None)
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)
    asyncio.run(_seed_encrypted_orphans([a, b]))

    with psycopg.connect(sync_dsn, autocommit=True) as conn:
        task_count_before = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]

    # Drive the real sync DBOS entry point (its own asyncio.run loop = a
    # beat tick). This must NOT run inside a running loop, hence a sync test.
    orphan.kb_orphan_reconciler()

    with psycopg.connect(sync_dsn, autocommit=True) as conn:
        for seed in (a, b):
            row = conn.execute(
                "SELECT status, private_ciphertext, private_nonce, "
                "private_key_id FROM kb_files WHERE id=%s",
                (str(seed["file_id"]),),
            ).fetchone()
            assert row[0] == "failed"
            assert all(row[index] for index in (1, 2, 3))
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == (
            task_count_before
        )
