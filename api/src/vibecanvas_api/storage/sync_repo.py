"""Synchronous bridge over the async :class:`WorkflowRepo`.

The agent's canvas tools are synchronous and call
``repo.commit`` / ``repo.get_current_workflow`` etc. synchronously —
typically off the event loop via ``asyncio.to_thread``
deleted the legacy daemon-thread bridge, but ``asyncio.to_thread``
still spawns a worker thread per call).

A request-scoped async session must not be shared across these
thread-bound short calls, and SSE handlers must not hold a session for
the stream lifetime. So this adapter opens a *short-lived* session for
**each** call (commit on success / rollback on error), mirroring the
per-write session pattern the spec mandates for streaming. The worker
thread has no running event loop, so each method drives the coroutine
via ``asyncio.run``.

Because async engines are loop-bound, this adapter must not
go through the process-global ``init_engine``/``session_scope`` — that
engine binds its asyncpg pool to the FIRST ``asyncio.run`` call's event
loop, so call #2 (a fresh loop) reuses connections owned by the
now-closed first loop and crashes with ``RuntimeError: Event loop is
closed`` / "attached to a different loop". The hot path makes 3
consecutive sync calls per agent auto-save turn (``agent.py``:
commit→mark_saved→get_current_workflow), and canvas tools do paired
commit+mark_saved, so this is not hypothetical. Instead each call builds
its OWN short-lived engine with ``NullPool`` (a real connection
opened+closed per use — acceptable at per-agent-turn / per-ref
frequency) and disposes it inside the SAME ``asyncio.run``, guaranteeing
zero loop-bound pooled state ever survives a call. ``db.py``'s global
``init_engine``/``session_scope`` are left untouched — they correctly
serve the async DI request path.

Only the methods the frozen agent + tools actually invoke are exposed
(verified by grep over agent.py / tools/*). Each returns the exact shape
the async repo returns, so callers are byte-compatible.
"""

from __future__ import annotations

from typing import Any

from vibecanvas_api.storage.sync_session import run_in_short_session
from vibecanvas_api.storage.workflow_repo import VersionPointer, WorkflowRepo


class SyncWorkflowRepo:
    """Sync facade; one short async session + NullPool engine per call."""

    def __init__(self, username: str):
        self._username = username

    def _run(self, method_name: str, *args, **kwargs) -> Any:
        # Delegate to the shared short-session driver
        # refactor): per-call NullPool engine + async session,
        # commit-on-success / rollback-on-exception / dispose-in-finally,
        # all inside one ``asyncio.run`` — byte-identical semantics to
        # the previous inline implementation
        # loop-bound-engine fix now lives in one place).
        return run_in_short_session(
            lambda s: getattr(
                WorkflowRepo(s, self._username), method_name)(
                    *args, **kwargs))

    # --- methods the frozen agent / tools call synchronously -----------

    def commit(self, wf_id: str, workflow: dict, note: str = "",
               editor: str = "") -> VersionPointer:
        return self._run("commit", wf_id, workflow, note=note, editor=editor)

    def new_version(self, wf_id: str, workflow: dict,
                    note: str = "New Major Version") -> int:
        return self._run("new_version", wf_id, workflow, note=note)

    def mark_saved(self, wf_id: str) -> None:
        return self._run("mark_saved", wf_id)

    def get_current_workflow(self, wf_id: str) -> dict:
        return self._run("get_current_workflow", wf_id)

    def get_workflow_at(self, wf_id: str, v: int, sv: int) -> dict:
        return self._run("get_workflow_at", wf_id, v, sv)

    def set_head(self, wf_id: str, major: int, sub: int) -> dict:
        """Move the active HEAD pointer to v{major}.sv{sub} and persist it (the
        same durable undo/redo/checkout the REST /undo /redo routes use). Returns
        the new meta."""
        return self._run("set_head", wf_id, major, sub)

    def get_meta(self, wf_id: str) -> dict:
        return self._run("get_meta", wf_id)

    def list_major_versions(self, wf_id: str) -> list[dict]:
        return self._run("list_major_versions", wf_id)

    def create_workflow(self, *, name: str = "", description: str = "",
                        domain: str = "public", tags: list | None = None,
                        initial_workflow: dict | None = None,
                        initial_note: str = "init") -> dict:
        return self._run(
            "create_workflow",
            name=name,
            description=description,
            creator_user_id=self._username,
            domain=domain,
            tags=tags or [],
            initial_workflow=initial_workflow,
            initial_note=initial_note,
        )
