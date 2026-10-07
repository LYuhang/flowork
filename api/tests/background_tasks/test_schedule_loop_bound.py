"""Remaining maintenance schedules survive repeated fresh event loops.

Exercise real synchronous entry points with per-call PostgreSQL sessions.
Task resubmission was removed; maintenance does not execute Workflow inputs.
"""
from __future__ import annotations


def _point_admin_engine_at_test_db(monkeypatch, pg_url: str) -> None:
    """Force every ``get_admin_engine`` call through the production path.

    Reset the cached singleton to ``None`` and set ``ADMIN_DATABASE_URL``
    to the superuser test DB, so the engine is (re)built from the env var
    exactly as in production — NOT swapped for a fixture engine bound to
    the test loop.
    """
    from vibecanvas_api.storage import db as db_mod

    monkeypatch.setattr(db_mod, "_admin_engine", None)
    # asyncpg URL form for create_async_engine.
    monkeypatch.setenv("ADMIN_DATABASE_URL", pg_url)


def test_kb_gc_sweeper_survives_two_beat_ticks(monkeypatch, pg_url):
    """Two sequential beat ticks of the KB GC sweeper must both succeed.

    ``kb_gc_sweeper`` opens an admin connection (phase-1 SELECT) AND an
    admin transaction (phase-3 DELETE) per tick — both through the
    process-global pool on broken code. With no doomed KBs seeded the
    body is a no-op, isolating the engine-lifecycle bug on tick #2.
    """
    import vibecanvas_api.background_tasks.kb_gc_sweeper as gc

    _point_admin_engine_at_test_db(monkeypatch, pg_url)

    gc.kb_gc_sweeper()
    gc.kb_gc_sweeper()


def test_kb_orphan_reconciler_survives_two_beat_ticks(monkeypatch, pg_url):
    """Two sequential beat ticks of the KB orphan reconciler must succeed.

    ``kb_orphan_reconciler`` opens an admin transaction (Case A UPDATE +
    Case B SELECT) per tick through the process-global pool on broken
    code. With no orphan rows seeded the per-row Case B write loop is
    skipped, isolating the admin-engine lifecycle on tick #2.
    """
    import vibecanvas_api.background_tasks.kb_orphan_reconciler as orphan

    _point_admin_engine_at_test_db(monkeypatch, pg_url)

    orphan.kb_orphan_reconciler()
    orphan.kb_orphan_reconciler()
