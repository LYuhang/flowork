"""Authorized task-result streaming endpoint.

See the Task lifecycle in ``docs/architecture.md``.
(D.3 — frontend detail page download button).

The route is a thin wrapper around :meth:`TasksRepo.get` + the object
store's bounded iterator. We exercise it at the repo + service seam (same
pattern as T11/T13) so the test suite stays asyncpg-friendly — the
``TestClient`` path goes through sync httpx which can't drive the
async-only session_scope code paths the live endpoint uses.

What we assert here:
  * A task row without ``results_uri`` (queued / running / cancelled
    before upload) → the endpoint's 404 precondition fires.
  * A task row with ``results_uri`` → the endpoint streams it through the
    authorized gateway and never mints a direct plaintext Object Store URL.
  * The route is mounted in the FastAPI app under
    ``/api/v1/tasks/{task_id}/download``.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text


def test_download_route_mounted_in_app():
    """The /download endpoint is registered in the OpenAPI route table."""
    from vibecanvas_api.app import build_app
    from vibecanvas_api.authorization.manifest import application_route_contexts

    app = build_app()
    paths = {r.path for r in application_route_contexts(app)}
    assert "/api/v1/tasks/{task_id}/download" in paths


@pytest.mark.asyncio
async def test_download_404_if_no_results(pg_engine):
    """A task without ``results_uri`` triggers the endpoint's 404 branch."""
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with pg_engine.begin() as c:
        await c.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'x')"),
            {"t": tenant_id},
        )
        await c.execute(
            text(
                "INSERT INTO users(user_id, tenant_id, email) "
                "VALUES (:u, :t, :e)"
            ),
            {
                "u": user_id,
                "t": tenant_id,
                "e": f"dl-{uuid.uuid4().hex[:6]}@example.com",
            },
        )

    # Seed a running task with no results_uri yet.
    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = TasksRepo(s)
        await repo.create(
            task_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workflow_id=None,
            task_type="batch_exec",
            payload={},
            background_job_id=str(task_id),
        )
        await repo.update_status(task_id, status="running")

    # Exercise the route's precondition at the repo seam: the endpoint
    # raises 404 iff `t is None or not t.results_uri`. We assert both
    # sides of the predicate by inspecting the row.
    async with session_scope(tenant_id=str(tenant_id)) as s:
        t = await TasksRepo(s).get(task_id)
        assert t is not None
        assert t.results_uri is None  # → endpoint would 404.


def test_uri_to_key_reverses_each_scheme():
    """The download route maps a stored ``results_uri`` back to its bare
    key to stream non-S3 blobs via ``fetch_bytes``."""
    from vibecanvas_api.services.object_store import uri_to_key

    assert uri_to_key("memory://tasks/abc/results.csv") == "tasks/abc/results.csv"
    assert uri_to_key("fs://tasks/abc/results.csv") == "tasks/abc/results.csv"
    assert uri_to_key("s3://my-bucket/tasks/abc/results.csv") == "tasks/abc/results.csv"


@pytest.mark.asyncio
async def test_download_streams_bytes_for_non_s3(pg_engine):
    """For non-S3 providers (filesystem / inmemory) the route streams the
    blob server-side via ``fetch_bytes`` — no signed URL exists.

    We assert the seam the route relies on: ``uri_to_key`` recovers the
    key from the stored ``results_uri``, and ``fetch_bytes(key)`` returns
    the exact bytes that were uploaded. That is what the route wraps in a
    ``Response(content=..., media_type="text/csv")``.
    """
    from vibecanvas_api.services.object_store import uri_to_key
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_tasks import TasksRepo
    from vibecanvas_api.services import object_store

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with pg_engine.begin() as c:
        await c.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'x')"),
            {"t": tenant_id},
        )
        await c.execute(
            text(
                "INSERT INTO users(user_id, tenant_id, email) "
                "VALUES (:u, :t, :e)"
            ),
            {
                "u": user_id,
                "t": tenant_id,
                "e": f"dl2-{uuid.uuid4().hex[:6]}@example.com",
            },
        )

    store = object_store.get_object_store()
    csv_bytes = b"i,output\n0,ok\n"
    uri = store.put_bytes(
        f"tasks/{task_id}/results.csv",
        csv_bytes,
        content_type="text/csv",
    )

    async with session_scope(tenant_id=str(tenant_id)) as s:
        repo = TasksRepo(s)
        await repo.create(
            task_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workflow_id=None,
            task_type="batch_exec",
            payload={},
            background_job_id=str(task_id),
        )
        from datetime import datetime, timezone
        await repo.update_status(
            task_id,
            status="finished",
            results_uri=uri,
            finished_at=datetime.now(timezone.utc),
        )

    async with session_scope(tenant_id=str(tenant_id)) as s:
        t = await TasksRepo(s).get(task_id)
        assert t is not None
        assert t.results_uri == uri

    # The route, for non-S3 providers, does
    # ``data = await asyncio.to_thread(store.fetch_bytes, uri_to_key(uri))``
    # then wraps it in ``Response(content=data, media_type="text/csv")``.
    # Assert that seam returns the exact uploaded bytes.
    key = uri_to_key(uri)
    assert store.fetch_bytes(key) == csv_bytes


@pytest.mark.asyncio
async def test_download_404_if_blob_missing(pg_engine):
    """FIX-2: a task whose ``results_uri`` is set but whose blob is ABSENT
    from the object store must return 404 (not a 500 from an uncaught
    ``KeyError`` in ``fetch_bytes``).

    We drive the real route coroutine ``download_results`` directly (the
    file's note: TestClient can't drive these async session paths) with a
    task row pointing at a key that was never written.
    """
    from vibecanvas_api.routes.tasks import download_results
    from vibecanvas_api.storage.db import session_scope
    from fastapi import HTTPException

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with pg_engine.begin() as c:
        await c.execute(
            text("INSERT INTO tenants(tenant_id, name) VALUES (:t, 'x')"),
            {"t": tenant_id},
        )
        await c.execute(
            text(
                "INSERT INTO users(user_id, tenant_id, email) "
                "VALUES (:u, :t, :e)"
            ),
            {"u": user_id, "t": tenant_id,
             "e": f"dl3-{uuid.uuid4().hex[:6]}@example.com"},
        )

    # results_uri points at a key that was NEVER put_bytes → fetch_bytes
    # raises KeyError inside the route.
    missing_uri = f"memory://tasks/{task_id}/never-written.csv"
    async with session_scope(tenant_id=str(tenant_id)) as s:
        from datetime import datetime, timezone
        from vibecanvas_api.storage.repo_tasks import TasksRepo

        repo = TasksRepo(s)
        await repo.create(
            task_id=task_id,
            tenant_id=tenant_id,
            user_id=user_id,
            workflow_id=None,
            task_type="batch_exec",
            payload={},
            background_job_id=str(task_id),
        )
        await repo.update_status(
            task_id,
            status="finished",
            results_uri=missing_uri,
            finished_at=datetime.now(timezone.utc),
        )

    async with session_scope(tenant_id=str(tenant_id)) as s:
        from types import SimpleNamespace
        from starlette.requests import Request
        from vibecanvas_api.auth.deps import AuthContext
        from vibecanvas_api.authorization.types import Decision

        class _AllowAuthzService:
            async def check(self, *_args, **_kwargs):
                return Decision(True, reason_code="test_allow")

        request = Request({
            "type": "http",
            "method": "GET",
            "path": f"/api/v1/tasks/{task_id}/download",
            "headers": [],
            "query_string": b"",
            "app": SimpleNamespace(
                state=SimpleNamespace(openfga_client=None),
            ),
            "state": {"request_id": "task-download-test"},
        })
        auth = AuthContext(
            user_id=str(user_id),
            tenant_id=str(tenant_id),
            email="",
            membership_status="active",
            membership_role="owner",
        )
        with pytest.raises(HTTPException) as exc:
            await download_results(
                task_id=task_id,
                request=request,
                ctx=auth,
                session=s,
                service=_AllowAuthzService(),
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_download_never_mints_plaintext_object_store_url(monkeypatch):
    from types import SimpleNamespace

    from starlette.requests import Request
    from starlette.responses import StreamingResponse

    from vibecanvas_api.auth.deps import AuthContext
    from vibecanvas_api.authorization.types import Decision
    from vibecanvas_api.routes import tasks as routes

    task_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()

    class _AllowAuthzService:
        async def check(self, *_args, **_kwargs):
            return Decision(True, reason_code="test_allow")

    class _Repo:
        async def get(self, _task_id):
            return SimpleNamespace(
                results_uri=f"s3://private/tasks/{task_id}/results.csv",
                result={},
            )

    class _Store:
        def signed_url(self, *_args, **_kwargs):
            raise AssertionError("private download must not mint a signed URL")

        def iter_bytes(self, key):
            assert key == f"tasks/{task_id}/results.csv"
            yield b"row,answer\n"
            yield b"1,ok\n"

    monkeypatch.setattr(routes, "TasksRepo", lambda _session: _Repo())
    monkeypatch.setattr(routes, "get_object_store", lambda: _Store())

    request = Request({
        "type": "http",
        "method": "GET",
        "path": f"/api/v1/tasks/{task_id}/download",
        "headers": [],
        "query_string": b"",
        "app": SimpleNamespace(state=SimpleNamespace(openfga_client=None)),
        "state": {"request_id": "task-download-stream-test"},
    })
    auth = AuthContext(
        user_id=str(user_id),
        tenant_id=str(tenant_id),
        email="",
        membership_status="active",
        membership_role="owner",
    )

    response = await routes.download_results(
        task_id=task_id,
        request=request,
        ctx=auth,
        session=object(),
        service=_AllowAuthzService(),
    )

    assert isinstance(response, StreamingResponse)
    assert response.headers["cache-control"] == "private, no-store"
    assert b"".join([chunk async for chunk in response.body_iterator]) == (
        b"row,answer\n1,ok\n"
    )
