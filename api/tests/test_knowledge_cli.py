import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text

from vibecanvas_api.flowork_cli import cli, knowledge_cli
from vibecanvas_api.services.agent_runtime import cli_knowledge as host
from vibecanvas_api.services import knowledge_packages as packages
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_kb import KbRepo


def encoded(path="README.md", data=b'---\nname: Package\ndescription: ""\n---\n# Package'):
    return {"path": path, "data": base64.b64encode(data).decode()}


@pytest.mark.asyncio
async def test_unexpected_host_failure_logs_locations_without_secret_values(monkeypatch):
    secret = "private-package-content-must-not-be-logged"
    monkeypatch.setattr(host.agent_context, "resolve_context", AsyncMock(side_effect=RuntimeError(secret)))
    logged = []
    monkeypatch.setattr(host.logger, "error", lambda event, **fields: logged.append((event, fields)))
    call = SimpleNamespace(operation="knowledge.list", capability=SimpleNamespace())
    result = await host.execute(call, {"limit": 20, "offset": 0})
    assert result["error"] == "knowledge_unavailable"
    assert logged[0][0] == "knowledge_cli_failed"
    assert logged[0][1]["error_type"] == "RuntimeError"
    assert logged[0][1]["publication_started"] is False
    assert logged[0][1]["frames"]
    assert secret not in json.dumps([result, logged])


def test_list_get_and_removed_commands(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cli, "request", lambda endpoint, args, **kw: seen.append((args, kw)) or {"status": "succeeded"})
    assert cli.main(["knowledge", "list"], socket_path="test") == 0
    assert seen[-1] == ({"limit": 20, "offset": 0}, {"operation": "knowledge.list"})
    identifier = str(uuid4())
    assert cli.main(["knowledge", "get", "--knowledge-id", identifier], socket_path="test") == 0
    for removed in ("search", "status", "upload", "update", "refresh", "files", "read"):
        assert cli.main(["knowledge", removed], socket_path="test") == 2
    capsys.readouterr()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,detail,expected,forbidden", [
    (409, "kb_name_conflict", "distinct name", "indexing"),
    (409, "kb_delete_in_progress", "Wait for indexing", "distinct name"),
    (404, "kb_not_found", "accessible knowledge_id", "deletion"),
    (403, "forbidden", "current permissions", "indexing"),
    (422, {"field": "name"}, "correct the reported arguments", "deletion"),
])
async def test_knowledge_http_error_hint_matches_the_actual_problem(
    monkeypatch, status, detail, expected, forbidden,
):
    from fastapi import HTTPException

    monkeypatch.setattr(host.agent_context, "resolve_context", AsyncMock(side_effect=HTTPException(status, detail)))
    call = SimpleNamespace(capability=SimpleNamespace(), operation="knowledge.create")
    result = await host.execute(call, {"files": [encoded()]})
    assert result["status"] == "failed"
    assert expected in result["hint"]
    assert forbidden not in result["hint"]


@pytest.mark.parametrize("action,args", [
    ("list", {"limit": 0}), ("list", {"offset": -1}), ("list", {"scope": "all"}),
    ("status", {"knowledge_id": "bad"}), ("status", {}),
    ("update", {"knowledge_id": str(uuid4())}),
    ("create", {"name": " "}), ("create", {"name": "x", "files": []}),
    ("upload", {"knowledge_id": str(uuid4()), "files": [encoded()], "expected_version": 1}),
    ("search", {"knowledge_id": [str(uuid4())], "query": "q", "limit": 21}),
    ("delete", {"knowledge_id": str(uuid4()), "confirm": True}),
])
def test_reject_invalid_requests(action, args):
    with pytest.raises(ValueError):
        cli.validate_arguments("knowledge." + action, args)


def test_check_and_create_require_source(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_bytes(base64.b64decode(encoded()["data"]))
    seen = []
    monkeypatch.setattr(cli, "request", lambda endpoint, args, **kw: seen.append(kw["operation"]) or {})
    for action in ("check", "create"):
        assert cli.main(["knowledge", action, "--source-dir", str(tmp_path)], socket_path="test") == 0
    assert seen == ["knowledge.check", "knowledge.create"]


def test_upload_captures_complete_tree_with_expected_version(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("# Package")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "binary.bin").write_bytes(bytes(range(256)))
    (tmp_path / ".hidden").write_text("included")
    seen = []
    monkeypatch.setattr(cli, "request", lambda endpoint, args, **kw: seen.append(args) or {"package_version": 8})
    assert cli.main(["knowledge", "publish", "--expected-version", "7", "--knowledge-id", str(uuid4()), "--source-dir", str(tmp_path)], socket_path="test") == 0
    assert set(seen[0]) == {"knowledge_id", "files", "expected_version"}
    assert {f["path"] for f in seen[0]["files"]} == {"README.md", "nested/binary.bin", ".hidden"}


@pytest.mark.parametrize("kind", ["missing_readme", "symlink", "fifo"])
def test_unsafe_local_package_never_dispatches(tmp_path, monkeypatch, kind):
    if kind != "missing_readme":
        (tmp_path / "README.md").write_text("# Package")
    if kind == "symlink":
        (tmp_path / "link").symlink_to(tmp_path / "README.md")
    if kind == "fifo":
        os.mkfifo(tmp_path / "pipe")
    monkeypatch.setattr(cli, "request", lambda *a, **kw: pytest.fail("must not dispatch"))
    assert cli.main(["knowledge", "create", "--source-dir", str(tmp_path)], socket_path="test") == 2


@pytest.mark.parametrize("files", [
    [encoded("../README.md")], [encoded("/README.md")],
    [encoded(), encoded("README.md/child")], [encoded(), encoded("readme.MD")],
    [encoded(), {"path": "file", "data": "invalid!"}],
])
def test_malicious_wire_files_rejected(files):
    with pytest.raises(ValueError):
        knowledge_cli.validate_files(files)


def test_download_no_overwrite_and_binary_roundtrip(tmp_path, monkeypatch, capsys):
    target = tmp_path / "download"
    files = [encoded(), encoded("nested/raw.bin", bytes(range(256)))]
    monkeypatch.setattr(cli, "request", lambda *a, **kw: {"files": files, "package_version": 2, "file_count": 2})
    command = ["knowledge", "download", "--knowledge-id", str(uuid4()), "--output-dir", str(target)]
    assert cli.main(command, socket_path="test") == 0
    assert (target / "nested/raw.bin").read_bytes() == bytes(range(256))
    assert target.stat().st_mode & 0o777 == 0o700
    assert "files" not in json.loads(capsys.readouterr().out)
    (target / "README.md").write_text("my local edits")
    assert cli.main(command, socket_path="test") == 2
    assert (target / "README.md").read_text() == "my local edits"


def test_materialize_rejects_symlinked_parent(tmp_path):
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    (target / "nested").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        knowledge_cli.materialize(str(target), [encoded(), encoded("nested/escape")])
    assert not (outside / "escape").exists()


def test_index_state_is_separate_from_publication():
    state = host.index_summary([SimpleNamespace(status=s) for s in ("indexed", "stored", "failed")])
    assert state["index_status"] == "failed" and not state["search_complete"]
    state = host.index_summary([SimpleNamespace(status="indexed")])
    assert state["search_complete"]
    assert host.publication("id", 3, [None])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_download_retries_changed_version_not_mixed_snapshot(monkeypatch):
    @asynccontextmanager
    async def scope(**kw):
        yield object()
    monkeypatch.setattr(host, "session_scope", scope)
    monkeypatch.setattr(host, "resource_route_params", lambda *a: {})
    monkeypatch.setattr(host, "authorize", AsyncMock())
    monkeypatch.setattr(host, "version", AsyncMock(side_effect=[1, 2, 2, 2]))
    monkeypatch.setattr(host, "KbRepo", lambda session: SimpleNamespace(get_active=AsyncMock(return_value=SimpleNamespace(name="Legacy", description=""))))
    snapshot = AsyncMock(side_effect=[[packages.PackageFile("README.md", b"old", "")], [packages.PackageFile("README.md", b"new", "")]])
    monkeypatch.setattr(host, "package_snapshot", snapshot)
    result = await host.read(SimpleNamespace(tenant_id="tenant"), "knowledge.download", {"knowledge_id": str(uuid4())})
    assert result["package_version"] == 2
    assert base64.b64decode(result["files"][0]["data"]).endswith(b"new")
    assert result["metadata_added_to_local_readme"] is True
    assert snapshot.await_count == 2


@pytest.mark.asyncio
async def test_denial_never_publishes(monkeypatch):
    from vibecanvas_api.agents.tools.decorator import ToolError
    from vibecanvas_api.services.agent_runtime import cli_delete
    @asynccontextmanager
    async def scope(**kw):
        yield SimpleNamespace(execute=AsyncMock())
    monkeypatch.setattr(host, "session_scope", scope)
    monkeypatch.setattr(host.agent_context, "resolve_context", AsyncMock(return_value=SimpleNamespace(tenant_id="tenant")))
    monkeypatch.setattr(host, "authorize", AsyncMock())
    monkeypatch.setattr(host, "resource_route_params", lambda *a: {})
    monkeypatch.setattr(cli_delete, "_approve", AsyncMock(side_effect=ToolError("approval_denied", "User declined. No changes were made.")))
    publish = AsyncMock()
    monkeypatch.setattr(host, "replace_package", publish)
    call = SimpleNamespace(operation="knowledge.publish", call_id="call", emit=AsyncMock(),
        capability=SimpleNamespace(tenant_id="tenant", turn_id="turn", approval_mode="agent"))
    result = await host.execute(call, {"knowledge_id": str(uuid4()), "files": [encoded()], "expected_version": 1})
    assert result["error"] == "approval_denied"
    publish.assert_not_awaited()


def test_large_package_has_no_four_mib_cli_cap(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("# Large")
    (tmp_path / "large.bin").write_bytes(b"x" * (5 * 1024 * 1024))
    def request(endpoint, args, **kw):
        assert len(base64.b64decode(next(f["data"] for f in args["files"] if f["path"] == "large.bin"))) == 5 * 1024 * 1024
        return {"package_version": 2}
    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["knowledge", "publish", "--expected-version", "7", "--knowledge-id", str(uuid4()), "--source-dir", str(tmp_path)], socket_path="test") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,action,approval", [
    ("agent", "create", True), ("agent", "publish", True), ("agent", "delete", True),
    ("always_allow", "publish", False), ("always_ask", "publish", True),
])
async def test_host_approval_and_frozen_publication(monkeypatch, mode, action, approval):
    from vibecanvas_api.services.agent_runtime import cli_delete
    ctx = SimpleNamespace(tenant_id=str(uuid4()), username=str(uuid4()))
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(first=lambda: (1,))), commit=AsyncMock())
    @asynccontextmanager
    async def scope(**kw):
        yield session
    monkeypatch.setattr(host, "session_scope", scope)
    monkeypatch.setattr(host.agent_context, "resolve_context", AsyncMock(return_value=ctx))
    authorize = AsyncMock()
    monkeypatch.setattr(host, "authorize", authorize)
    monkeypatch.setattr(host, "_require_active_chat_write", AsyncMock())
    monkeypatch.setattr(host, "resource_route_params", lambda *a: {"request": SimpleNamespace(state=SimpleNamespace())})
    approve = AsyncMock()
    monkeypatch.setattr(cli_delete, "_approve", approve)
    replace = AsyncMock(return_value=(9, []))
    monkeypatch.setattr(host, "replace_package", replace)
    monkeypatch.setattr(host, "enqueue_package_indexing", AsyncMock())
    row = SimpleNamespace(id=str(uuid4()), package_version=1)
    monkeypatch.setattr(host.routes, "_create_knowledge_package", AsyncMock(return_value=row))
    monkeypatch.setattr(host.routes, "update_kb", AsyncMock(return_value={"id": row.id}))
    monkeypatch.setattr(host.routes, "delete_kb", AsyncMock())
    call = SimpleNamespace(operation="knowledge." + action, call_id="call", emit=AsyncMock(),
        capability=SimpleNamespace(tenant_id=ctx.tenant_id, user_id=str(uuid4()), turn_id="run", approval_mode=mode))
    args = {"files": [encoded()]} if action == "create" else {"knowledge_id": row.id}
    if action == "update":
        args["description"] = ""
    if action == "publish":
        args["files"] = [encoded()]
        args["expected_version"] = 8
    result = await host.execute(call, args)
    assert result["status"] == "succeeded", result
    assert approve.await_count == int(approval)
    assert authorize.await_count == 2
    if action == "publish":
        assert replace.await_args.kwargs["expected_version"] == 8
        assert replace.await_args.kwargs["protect_draft"] is True
        assert replace.await_args.kwargs["files"][0].data == b'---\nname: Package\ndescription: ""\n---\n# Package'
        if approval:
            summary = approve.await_args.args[1]
            assert "files" not in summary and summary["file_count"] == 1


@pytest.mark.asyncio
async def test_concurrent_uploads_increment_and_rollback_atomically(pg_engine, monkeypatch):
    tenant, user = uuid4(), uuid4()
    async with pg_engine.begin() as connection:
        await connection.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'Knowledge CLI test')"), {"t": tenant})
        await connection.execute(text("INSERT INTO users(user_id,tenant_id,email) VALUES (:u,:t,:e)"), {"u": user, "t": tenant, "e": str(user) + "@example.com"})
    blobs = {}
    def put(key, data, content_type):
        if data.endswith(b"FAIL"):
            raise OSError("simulated object write failure")
        blobs[key] = data
    monkeypatch.setattr(packages, "get_object_store", lambda: SimpleNamespace(put_bytes=put, fetch_bytes=blobs.__getitem__))
    async with session_scope(tenant_id=str(tenant)) as session:
        kb = await KbRepo(session).create_kb(tenant_id=tenant, user_id=user, name="CLI package")
        identifier = kb.id
        await packages.replace_package(session, kb_id=identifier, actor_user_id=user, expected_version=1,
            files=[packages.PackageFile("README.md", b"initial", "")], increment_version=False, derive_index=False)
    async def publish(content, expected=1):
        async with session_scope(tenant_id=str(tenant)) as session:
            # Prime ORM identity map with an older version before acquiring lock.
            cached = await KbRepo(session).get_active(identifier)
            assert cached
            return (await packages.replace_package(session, kb_id=identifier, actor_user_id=user,
                expected_version=expected, files=[packages.PackageFile("README.md", content, "")], derive_index=False))[0]
    from vibecanvas_api.services.write_conflicts import WriteConflict
    versions = await asyncio.gather(publish(b"second"), publish(b"third"), return_exceptions=True)
    assert sum(v == 2 for v in versions) == 1
    conflicts = [v for v in versions if isinstance(v, WriteConflict)]
    assert len(conflicts) == 1
    assert conflicts[0].detail['current_version'] == 2
    async with session_scope(tenant_id=str(tenant)) as session:
        before = await packages.package_snapshot(session, identifier)
    with pytest.raises(OSError):
        await publish(b"FAIL", expected=2)
    async with session_scope(tenant_id=str(tenant)) as session:
        kb = await KbRepo(session).get_active(identifier)
        assert kb.package_version == 2
        assert await packages.package_snapshot(session, identifier) == before
