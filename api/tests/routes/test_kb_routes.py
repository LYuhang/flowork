"""KB/RAG T6 — ``routes/kb.py`` smoke tests.

Strategy: handler-direct-call (mirrors ``test_mcp_servers_create.py`` +
``test_kb_indexer.py``). The plan's ``client`` + ``auth_headers``
fixtures do not exist in this repo's conftest; this codebase consistently
calls the route handler functions directly with a stub ``AuthContext``
and a manually-opened ``session_scope(tenant_id=...)``. That tests the
same body validation, package-path identity, size, and MIME/parser paths
without bringing up the auth stack.

Coverage includes:

1. ``test_create_and_list_kb`` — POST /kb + GET /kb round-trip writes
   and reads the same row.
2. ``test_upload_duplicate_content_as_distinct_files`` — identical bytes may
   be present at distinct package paths.
3. ``test_upload_too_large_413`` — payload > 50 MB raises 413
   ``kb_file_too_large``.
4. ``test_unindexed_type_is_stored`` — files outside the parser registry are
   retained as authoritative package content without a derived index.

Upload cases also patch the background enqueue boundary and ``get_object_store``
so the test doesn't actually hit the broker / boto3 client.
"""
from __future__ import annotations

import io
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import text

from vibecanvas_api.authorization.types import Action, Decision
from vibecanvas_api.routes.kb import (
    KbCreate,
    create_kb,
    delete_file,
    import_kb,
    list_kbs,
    upload_file,
)
from vibecanvas_api.storage.repo_kb import KbRepo
from vibecanvas_api.storage.db import session_scope


# --------------------------------------------------------------------- seed


async def _seed_tenant_and_user(pg_engine, tenant_id, user_id) -> None:
    """Insert tenant + user via the RLS-bypassing superuser engine. Auth
    tables are RLS-free so a plain ``begin()`` block is fine. Matches the
    seeding pattern from ``test_kb_indexer.py`` / ``test_repo_kb.py``."""
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
             "e": f"kb-routes-{uuid.uuid4().hex[:6]}@example.com"},
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
    read ``tenant_id`` / ``user_id`` (both strings); ``email`` is unused."""

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
        self.state = SimpleNamespace(request_id="kb-route-test")
        self.app = SimpleNamespace(state=SimpleNamespace(openfga_client=None))


class _AllowAuthz:
    def __init__(self, resource_ids=()):
        self._resource_ids = tuple(str(value) for value in resource_ids)

    async def check(self, *args, **kwargs):
        return Decision(
            allowed=True,
            capabilities=frozenset(Action),
            effective_role="manager",
            reason_code="test_fixture",
        )

    async def list_authorized_ids(self, *args, **kwargs):
        return self._resource_ids

    async def batch_check(self, checks):
        return tuple(
            Decision(
                allowed=True,
                capabilities=frozenset(Action),
                effective_role="manager",
                reason_code="test_fixture",
            )
            for _ in checks
        )


def _make_upload(name: str, content: bytes, content_type: str) -> UploadFile:
    """Build a starlette UploadFile from in-memory bytes — matches the
    shape FastAPI hands to ``upload_file`` after multipart parsing."""
    return UploadFile(
        filename=name,
        file=io.BytesIO(content),
        headers={"content-type": content_type},
    )


# --------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_create_and_list_kb(pg_engine):
    """POST /kb followed by GET /kb returns the just-created row.

    Exercises the create_kb -> list_kbs roundtrip on a real DB so the
    UNIQUE-name partial index, the soft-delete filter, and the RLS
    binding all participate.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)

    ctx = _StubCtx(tenant_id, user_id)
    async with session_scope(tenant_id=str(tenant_id)) as s:
        created = await create_kb(
            body=KbCreate(name="HR", description="x"),
            request=_StubRequest(), ctx=ctx, session=s,
            service=_AllowAuthz(),
        )
        await s.commit()

    assert created.name == "HR"
    assert created.description == "x"
    kb_id = created.id

    async with session_scope(tenant_id=str(tenant_id)) as s:
        rows = await list_kbs(
            request=_StubRequest(), ctx=ctx, session=s,
            service=_AllowAuthz((kb_id,)),
        )

    assert any(k.id == kb_id for k in rows), (
        f"Expected KB {kb_id} in list, got {[k.id for k in rows]}"
    )


@pytest.mark.asyncio
async def test_upload_duplicate_content_as_distinct_files(pg_engine):
    """Package paths, rather than content hashes, define file identity."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    ctx = _StubCtx(tenant_id, user_id)

    async with session_scope(tenant_id=str(tenant_id)) as s:
        kb = await create_kb(
            body=KbCreate(name="X"), request=_StubRequest(),
            ctx=ctx, session=s, service=_AllowAuthz(),
        )
        await s.commit()

    blob = b"hello world content here"
    sent: list[dict] = []
    with patch(
        "vibecanvas_api.routes.kb.enqueue_background_job_async",
        side_effect=lambda *a, **kw: sent.append({"args": a, "kwargs": kw}),
    ):
        # First upload — succeeds, returns pending.
        async with session_scope(tenant_id=str(tenant_id)) as s:
            up1 = _make_upload("a.txt", blob, "text/plain")
            r1 = await upload_file(
                kb_id=uuid.UUID(kb.id), request=_StubRequest(), file=up1,
                ctx=ctx, session=s, service=_AllowAuthz(),
            )
        assert r1["status"] == "pending"
        assert "file_id" in r1

        # The same bytes at another path remain a valid package file.
        async with session_scope(tenant_id=str(tenant_id)) as s:
            up2 = _make_upload("copy.txt", blob, "text/plain")
            r2 = await upload_file(
                kb_id=uuid.UUID(kb.id), request=_StubRequest(), file=up2,
                ctx=ctx, session=s, service=_AllowAuthz(),
            )

    assert r2["status"] == "pending"
    assert r2["file_id"] != r1["file_id"]
    assert len(sent) == 2
    assert sent[0]["kwargs"]["kwargs"] == {"file_id": r1["file_id"]}
    assert sent[1]["kwargs"]["kwargs"] == {"file_id": r2["file_id"]}


@pytest.mark.asyncio
async def test_import_folder_creates_one_authoritative_package(pg_engine):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    ctx = _StubCtx(tenant_id, user_id)

    with patch(
        "vibecanvas_api.services.knowledge_packages.enqueue_background_job_async",
    ):
        async with session_scope(tenant_id=str(tenant_id)) as s:
            created = await import_kb(
                request=_StubRequest(),
                name="Research",
                description="A complete package",
                archive=None,
                files=[
                    _make_upload("README.md", b"# Research", "text/markdown"),
                    _make_upload("paper.pdf", b"%PDF-1.7", "application/pdf"),
                ],
                paths=["research/README.md", "research/papers/paper.pdf"],
                ctx=ctx,
                session=s,
                service=_AllowAuthz(),
            )

    async with session_scope(tenant_id=str(tenant_id)) as s:
        stored = await KbRepo(s).list_files(uuid.UUID(created.id))
    assert created.name == "Research"
    assert [item.name for item in stored] == ["README.md", "papers/paper.pdf"]


@pytest.mark.asyncio
async def test_package_rejects_duplicate_path_and_root_readme_delete(pg_engine):
    """Browser mutations cannot violate the package path/README invariants."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    ctx = _StubCtx(tenant_id, user_id)
    async with session_scope(tenant_id=str(tenant_id)) as s:
        kb = await create_kb(
            body=KbCreate(name="Invariant"), request=_StubRequest(),
            ctx=ctx, session=s, service=_AllowAuthz(),
        )
        await s.commit()

    async with session_scope(tenant_id=str(tenant_id)) as s:
        with pytest.raises(HTTPException) as duplicate:
            await upload_file(
                kb_id=uuid.UUID(kb.id), request=_StubRequest(),
                file=_make_upload("readme.MD", b"replacement", "text/markdown"),
                ctx=ctx, session=s, service=_AllowAuthz(),
            )
    assert duplicate.value.status_code == 409
    assert duplicate.value.detail == "knowledge_package_path_exists"

    async with session_scope(tenant_id=str(tenant_id)) as s:
        readme = next(
            item for item in await KbRepo(s).list_files(uuid.UUID(kb.id))
            if item.name == "README.md"
        )
        with pytest.raises(HTTPException) as required:
            await delete_file(
                kb_id=uuid.UUID(kb.id), file_id=readme.id,
                request=_StubRequest(), ctx=ctx, session=s,
                service=_AllowAuthz(),
            )
    assert required.value.status_code == 409
    assert required.value.detail == "knowledge_root_readme_required"


@pytest.mark.asyncio
async def test_upload_too_large_413(pg_engine):
    """A 51 MB payload → 413 ``kb_file_too_large`` BEFORE any DB write
    or object-store write happens. Step 1 of the upload pipeline."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    ctx = _StubCtx(tenant_id, user_id)

    async with session_scope(tenant_id=str(tenant_id)) as s:
        kb = await create_kb(
            body=KbCreate(name="X"), request=_StubRequest(),
            ctx=ctx, session=s, service=_AllowAuthz(),
        )
        await s.commit()

    blob = b"x" * (51 * 1024 * 1024)
    async with session_scope(tenant_id=str(tenant_id)) as s:
        up = _make_upload("big.txt", blob, "text/plain")
        with pytest.raises(HTTPException) as exc_info:
            await upload_file(
                kb_id=uuid.UUID(kb.id), request=_StubRequest(), file=up,
                ctx=ctx, session=s, service=_AllowAuthz(),
            )

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail == "kb_file_too_large"


@pytest.mark.asyncio
async def test_unindexed_type_is_stored(pg_engine):
    """An arbitrary binary remains in the package without an index task."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await _seed_tenant_and_user(pg_engine, tenant_id, user_id)
    ctx = _StubCtx(tenant_id, user_id)

    async with session_scope(tenant_id=str(tenant_id)) as s:
        kb = await create_kb(
            body=KbCreate(name="X"), request=_StubRequest(),
            ctx=ctx, session=s, service=_AllowAuthz(),
        )
        await s.commit()

    sent: list[dict] = []
    with patch(
        "vibecanvas_api.routes.kb.enqueue_background_job_async",
        side_effect=lambda *a, **kw: sent.append({"args": a, "kwargs": kw}),
    ):
        async with session_scope(tenant_id=str(tenant_id)) as s:
            up = _make_upload("diagram.drawio", b"<mxfile/>", "application/xml")
            result = await upload_file(
                kb_id=uuid.UUID(kb.id), request=_StubRequest(), file=up,
                ctx=ctx, session=s, service=_AllowAuthz(),
            )
    assert result["status"] == "stored"
    assert result["task_id"] is None
    assert sent == []


# --------------------------------------------------------------- router mount


def test_router_mounted_under_api_v1_kb():
    """The kb router is registered on ``build_app()`` so the upload /
    search / CRUD endpoints are reachable in production."""
    from vibecanvas_api.app import build_app
    from vibecanvas_api.authorization.manifest import application_route_contexts

    app = build_app()
    paths = [r.path for r in application_route_contexts(app)]
    assert "/api/v1/kb" in paths
    assert "/api/v1/kb/{kb_id}" in paths
    assert "/api/v1/kb/{kb_id}/files" in paths
    assert "/api/v1/kb/{kb_id}/files/{file_id}/raw" in paths
    assert "/api/v1/kb/search" in paths
