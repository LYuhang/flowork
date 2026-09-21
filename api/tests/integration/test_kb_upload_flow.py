"""Knowledge upload → normalize → encrypted grep end-to-end flow.

Wires together (in order):

* ``routes.kb.create_kb`` — KB row inserted.
* ``routes.kb.upload_file`` — file row + object store + durable enqueue.
* The DBOS ``kb.index_file`` ID adapter and runtime-neutral body — run the
  indexer against the
  in-memory object store. Status walks
  pending → indexing → indexed.
* ``routes.kb.search`` — issues a real encrypted lexical query against the
  freshly-inserted chunks and returns hits.

Background-worker strategy
--------------------------
Patch the runtime-neutral enqueue boundary with an async callable that invokes
the real opaque-ID adapter in a worker thread. This verifies that tenant/user
context is reloaded from business storage without requiring a worker process.

"""
from __future__ import annotations

import asyncio
import io
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import UploadFile
from sqlalchemy import text

from vibecanvas_api.authorization.types import Decision
from vibecanvas_api.background_workflows import _run_kb_from_id
from vibecanvas_api.routes.kb import (
    KbCreate,
    SearchRequest,
    create_kb,
    list_files,
    search,
    upload_file,
)
from vibecanvas_api.storage.db import session_scope


# --------------------------------------------------------------------- seed


async def _seed_tenant_and_user(pg_engine, tenant_id, user_id) -> None:
    """Insert tenant + user via the RLS-bypassing superuser engine
    (T1/T4/T5 pattern carried into T12)."""
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
             "e": f"kb-upflow-{uuid.uuid4().hex[:6]}@example.com"},
        )
        await c.execute(
            text(
                "INSERT INTO organizations("
                "tenant_id, kind, slug, name, created_by"
                ") VALUES (:t, 'personal', :slug, 'Test account', :u)"
            ),
            {"t": tenant_id, "u": user_id, "slug": f"test-{tenant_id.hex}"},
        )


class _StubCtx:
    """Lightweight stand-in for ``AuthContext``. The KB handlers only
    read ``tenant_id`` + ``user_id`` (both strings)."""

    def __init__(self, tenant_id, user_id):
        self.tenant_id = str(tenant_id)
        self.user_id = str(user_id)
        self.active_organization_id = str(tenant_id)
        self.session_id = "test-session"
        self.session_generation = 1
        self.membership_id = "test-membership"
        self.membership_role = "owner"
        self.membership_status = "active"
        self.authentication_strength = "password"
        self.email = "stub@example.com"


class _StubRequest:
    def __init__(self):
        self.headers = {}
        self.client = None
        self.state = SimpleNamespace(request_id="kb-upload-flow")
        self.app = SimpleNamespace(state=SimpleNamespace(openfga_client=None))


class _AllowAuthz:
    def __init__(self, resource_ids=()):
        self._resource_ids = tuple(str(value) for value in resource_ids)

    async def check(self, *args, **kwargs):
        return Decision(allowed=True, reason_code="test_fixture")

    async def list_authorized_ids(self, *args, **kwargs):
        return self._resource_ids

    async def batch_check(self, checks):
        return tuple(
            Decision(allowed=True, reason_code="test_fixture")
            for _ in checks
        )


def _make_upload(name: str, content: bytes, content_type: str) -> UploadFile:
    return UploadFile(
        filename=name,
        file=io.BytesIO(content),
        headers={"content-type": content_type},
    )


def _make_enqueue_runner():
    """Build an async enqueue replacement that runs the task body in a thread.

    Signature mirrors the way the upload route calls it::

        enqueue_background_job_async(
            "kb.index_file",
            job_id=...,
            queue=...,
            kwargs={"file_id": ...},
        )

    Only ``"kb.index_file"`` is dispatched — any other name is a bug
    in the test wiring and raises.
    """

    async def _run(name, *args, **kwargs):
        if name != "kb.index_file":
            raise AssertionError(f"unexpected background task: {name!r}")
        task_kwargs = kwargs.get("kwargs") or {}
        return await asyncio.to_thread(_run_kb_from_id, **task_kwargs)

    return _run


# --------------------------------------------------------------------- test


@pytest.mark.asyncio
async def test_upload_to_indexed_to_search(pg_engine, pg_url):
    """Create KB → upload txt → run indexer inline → search returns hits.

    The path is local and deterministic: parsing, encrypted chunk writes, and
    lexical retrieval require no model key or network call.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    ctx = _StubCtx(tenant_id, user_id)

    # 1. Create KB.
    async with session_scope(tenant_id=str(tenant_id)) as s:
        kb = await create_kb(
            body=KbCreate(name="Flow"), request=_StubRequest(),
            ctx=ctx, session=s, service=_AllowAuthz(),
        )
        await s.commit()
        kb_id = uuid.UUID(kb.id)

    # 2. Upload file — patch enqueue so the business body runs inline.
    blob = b"hello world this is the integration test content for KB upload flow"
    send_task_runner = _make_enqueue_runner()

    async def _allow_captured_user(*args, **kwargs):
        return None

    with patch(
        "vibecanvas_api.routes.kb.enqueue_background_job_async",
        side_effect=send_task_runner,
    ), patch(
        "vibecanvas_api.storage.sync_session._admin_url",
        return_value=pg_url,
    ), patch(
        "vibecanvas_api.background_tasks.kb_indexer._require_captured_user_update",
        side_effect=_allow_captured_user,
    ):
        async with session_scope(tenant_id=str(tenant_id)) as s:
            up = _make_upload("hello.txt", blob, "text/plain")
            r1 = await upload_file(
                kb_id=kb_id, request=_StubRequest(), file=up,
                ctx=ctx, session=s, service=_AllowAuthz(),
            )
    assert r1["status"] == "pending"
    file_id = r1["file_id"]

    # 3. Verify file walked pending → indexing → indexed (the eager-run
    # task body has already finished by the time send_task returns).
    # ``list_files`` signature uses ``Query(alias="status")`` for the
    # filter — we MUST pass ``file_status=None`` explicitly, else FastAPI's
    # default Query object gets bound to the parameter and asyncpg
    # chokes trying to encode it as a VARCHAR.
    async with session_scope(tenant_id=str(tenant_id)) as s:
        files = await list_files(
            kb_id=kb_id, request=_StubRequest(), file_status=None,
            ctx=ctx, session=s, service=_AllowAuthz(),
        )
    assert any(
        f.id == file_id and f.status == "indexed" for f in files
    ), (
        f"expected file {file_id} indexed, got "
        f"{[(f.id, f.status, f.error_message) for f in files]}"
    )

    # 4. Search normalized source text.
    async with session_scope(tenant_id=str(tenant_id)) as s:
        search_body = SearchRequest(
            kb_ids=[str(kb_id)], query="hello world", top_k=5,
        )
        search_resp = await search(
            body=search_body, request=_StubRequest(), ctx=ctx, session=s,
            service=_AllowAuthz(),
        )
    assert len(search_resp["results"]) >= 1, (
        f"expected at least 1 hit, got {search_resp}"
    )
