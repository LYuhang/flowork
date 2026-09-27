"""Host artifact authorization and byte-receipt tests, not browser acceptance."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.services.agent_runtime import browser_cli_authorization as authorization
from vibecanvas_api.services.sandbox import coordinator


ARTIFACT = {"file": "/data/download.bin", "bytes": 4, "sha256": "a" * 64}


@pytest.fixture
def host(monkeypatch):
    capability = SimpleNamespace(organization_id="tenant", workspace_scope_id="chat-workspace")
    monkeypatch.setattr(authorization, "verify_agent_capability", lambda *_args, **_kwargs: capability)
    context = AsyncMock()
    monkeypatch.setattr(authorization, "resolve_context", context)
    sandbox = SimpleNamespace(sync_workspace_path=AsyncMock(return_value=True))
    manager = SimpleNamespace(get_loaded_session=AsyncMock(return_value=sandbox))
    monkeypatch.setattr(coordinator, "get_sandbox_coordinator", lambda: manager)
    return context, sandbox, manager


async def commit(artifacts):
    return await authorization.commit_browser_artifacts(
        operation="browser.download", arguments={"tab_id": "tab_test", "locator": "#download", "file": "/data/download.bin"},
        artifacts=artifacts, token="private",
    )


@pytest.mark.asyncio
async def test_commits_exact_bytes_only_to_authorized_chat(host):
    context, sandbox, manager = host
    result = await commit([ARTIFACT])
    assert result == {"artifacts": [{**ARTIFACT, "persistence": "durable"}]}
    context.assert_awaited_once()
    manager.get_loaded_session.assert_awaited_once_with("tenant", "chat-workspace")
    sandbox.sync_workspace_path.assert_awaited_once_with(ARTIFACT["file"], expected_sha256=ARTIFACT["sha256"], expected_bytes=4)


@pytest.mark.asyncio
async def test_permission_revocation_cannot_commit(host):
    context, sandbox, _ = host
    context.side_effect = PermissionError("Revoked")
    with pytest.raises(PermissionError):
        await commit([ARTIFACT])
    sandbox.sync_workspace_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_persistence_failure_reports_local_file_without_retry(host):
    _, sandbox, _ = host
    sandbox.sync_workspace_path.return_value = False
    result = await commit([ARTIFACT])
    assert result["error"] == "artifact_persistence_failed"
    assert result["unconfirmed_artifact"] == ARTIFACT
    assert result["artifacts"] == []
    assert sandbox.sync_workspace_path.await_count == 1


@pytest.mark.asyncio
async def test_temporary_file_does_not_claim_durable_persistence(host):
    _, sandbox, _ = host
    result = await commit([{**ARTIFACT, "file": "/tmp/download.bin"}])
    assert result["artifacts"][0]["persistence"] == "sandbox"
    assert "Use /data" in result["artifacts"][0]["warning"]
    sandbox.sync_workspace_path.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact", [
    {**ARTIFACT, "file": "/data/../secret"}, {**ARTIFACT, "file": "relative"},
    {**ARTIFACT, "sha256": "invalid"}, {**ARTIFACT, "bytes": -1},
    {**ARTIFACT, "bytes": True}, "file",
])
async def test_invalid_manifest_is_rejected_before_storage(host, artifact):
    _, sandbox, manager = host
    with pytest.raises(ValueError):
        await commit([artifact])
    sandbox.sync_workspace_path.assert_not_awaited()
    manager.get_loaded_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_cookie_export_cannot_use_public_artifact_persistence(host):
    _, sandbox, _ = host
    with pytest.raises(PermissionError):
        await authorization.commit_browser_artifacts(
            operation="browser.cookie-export", arguments={"tab_id": "tab_test", "file": "cookies.json"},
            artifacts=[ARTIFACT], token="private",
        )
    sandbox.sync_workspace_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_receipt_checks_exact_bytes_and_rejects_links(tmp_path, monkeypatch):
    import hashlib
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    from vibecanvas_api.services.sandbox import manager as storage

    folder = tmp_path / "data"
    folder.mkdir()
    content = b"download\x00\xff"
    (folder / "file.bin").write_bytes(content)
    (folder / "link.bin").symlink_to(folder / "file.bin")
    session = storage.SandboxSession(
        tenant_id="tenant", wf_id="chat", run_dir=str(tmp_path), overlay_dir=None,
        provider=MagicMock(), base_binds=[], expose_run=False,
    )
    repo = SimpleNamespace(upsert_artifact_bytes=AsyncMock())

    @asynccontextmanager
    async def scope(**_kwargs):
        yield object()

    monkeypatch.setattr(storage, "short_session_scope", scope)
    monkeypatch.setattr(storage, "VfsRepo", lambda *_args, **_kwargs: repo)
    monkeypatch.setattr(storage, "get_object_store", lambda: object())
    digest = hashlib.sha256(content).hexdigest()
    assert not await session.sync_workspace_path("/data/file.bin", expected_sha256="0" * 64)
    assert not await session.sync_workspace_path("/data/file.bin", expected_bytes=len(content) + 1)
    assert not await session.sync_workspace_path("/data/link.bin", expected_sha256=digest)
    repo.upsert_artifact_bytes.assert_not_awaited()
    assert await session.sync_workspace_path("/data/file.bin", expected_sha256=digest, expected_bytes=len(content))
    assert repo.upsert_artifact_bytes.await_args.kwargs["data"] == content
