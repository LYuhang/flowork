"""Shared service-locator singletons used by the FastAPI route modules.

``init_stores`` is called once from ``app.py``'s lifespan to wire the
process-wide singletons the routes read directly (currently ``ctx.vfs_store``).

The legacy Gradio-era session-state factory
(``build_session_state``), the disk-checkpointer history loader
(``load_history_for_sync``), and the file-storage workspace/permission
helpers (``check_edit_permission``, ``workspace_items``, ``_build_card``,
``merge_meta_for_frontend``, ``full_meta_from_entry``,
``list_platform_users``, ``get_major_versions``, ``get_active_repo``,
``build_workflow_sync``) were removed. They constructed the now-deleted
file-backed stores / old-signature ``ChatRepo``/``ExecutionRepo`` and
the deleted ``PermissionService``, and had ZERO live callers in
``routes/`` / ``app.py`` (grep-verified) — they were dead code from the
Phase-2.2 HTTP cutover. The ``paths`` / ``storage_root`` /
``checkpointer_path`` module slots were likewise unreferenced by any
route and are dropped.

The legacy ``task_manager`` slot was deleted along with
the file-backed task manager (workers/* + managers/task_manager.py);
batch execution now goes through DBOS + the Postgres ``tasks`` table
(routes/tasks.py).
"""

import uuid

# ---------------------------------------------------------------------------
# Module-level store references (populated by init_stores; read by routes)
# ---------------------------------------------------------------------------
vfs_store = None


def init_stores(
    _vfs_store=None,
):
    """Called once by app.py's lifespan to inject shared singletons."""
    global vfs_store
    vfs_store = _vfs_store


def clear_stores() -> None:
    """Release process-wide references owned by a finished app lifespan.

    ``build_app()`` may be entered more than once in the same process (tests,
    embedded servers, or an in-process restart).  Keeping the old references
    after its connection pool closes makes later requests try to use a closed
    checkpointer.  The identity guard prevents an older lifespan from clearing
    stores that a newer lifespan has already installed.
    """
    global vfs_store
    vfs_store = None


# ---------------------------------------------------------------------------
# Signal helper (used by routes/chats.py SSE envelope)
# ---------------------------------------------------------------------------

def build_signal(signal_type: str, payload: dict) -> dict:
    return {
        "__signal_id__": str(uuid.uuid4()),
        "type": signal_type,
        "payload": payload,
    }
