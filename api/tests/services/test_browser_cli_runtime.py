"""Worker lifecycle checks, not real-browser acceptance evidence."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.services.agent_runtime import browser_cli_runtime as module


MATERIAL = {"endpoint": "ws://private", "bearer": "private", "fence": ["session", 1]}
MUTATION = ("browser.click", {"tab_id": "tab_test", "locator": "button"})


def worker(monkeypatch, authorize=None, on_write=None):
    runtime = module.BrowserCliRuntime(authorize or AsyncMock(return_value=MATERIAL))
    reader = asyncio.StreamReader()
    frames = []

    def write(data):
        frame = json.loads(data)
        frames.append(frame)
        if on_write:
            on_write(reader, frame)

    process = SimpleNamespace(
        returncode=None, stdout=reader,
        stdin=SimpleNamespace(write=write, drain=AsyncMock()),
    )

    async def start(material):
        runtime._process = process
        runtime._fence = material["fence"]

    async def stop():
        runtime._process = None
        runtime._fence = None

    monkeypatch.setattr(runtime, "_start", AsyncMock(side_effect=start))
    monkeypatch.setattr(runtime, "_stop", AsyncMock(side_effect=stop))
    return runtime, reader, frames, process


def reply(reader, frame, result=None):
    reader.feed_data(json.dumps({"id": frame["id"], "result": result or {"status": "succeeded"}}).encode() + b"\n")


@pytest.mark.asyncio
async def test_reuses_worker_but_authorizes_every_command(monkeypatch):
    runtime, _, frames, _ = worker(monkeypatch, on_write=reply)
    for _ in range(2):
        assert (await runtime.execute(*MUTATION, AsyncMock()))["status"] == "succeeded"
    assert runtime.authorize.await_count == 2
    assert runtime._start.await_count == 1
    assert [frame["id"] for frame in frames] == [1, 2]


@pytest.mark.asyncio
async def test_denial_closes_existing_connection_without_dispatch(monkeypatch):
    runtime, _, frames, process = worker(monkeypatch, authorize=AsyncMock(return_value={"error": "permission_denied"}))
    runtime._process = process
    result = await runtime.execute(*MUTATION, AsyncMock())
    assert result == {"status": "failed", "error": "permission_denied"}
    runtime._stop.assert_awaited_once()
    assert not frames


@pytest.mark.asyncio
async def test_startup_failure_is_not_an_unknown_mutation(monkeypatch):
    runtime, _, frames, _ = worker(monkeypatch)
    runtime._start.side_effect = ConnectionError("Cannot initialize")
    result = await runtime.execute(*MUTATION, AsyncMock())
    assert result["status"] == "failed"
    assert result["error"] == "browser_unavailable"
    assert not frames
    runtime._stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_diagnostic_is_actionable_and_contains_no_connection_credentials(monkeypatch):
    diagnostic = module._startup_diagnostic(
        "Unexpected close at ws://private; Bearer private; local-token",
        MATERIAL, "local-token",
    )
    assert "private" not in diagnostic
    assert "local-token" not in diagnostic
    assert "Unexpected close" in diagnostic
    runtime, _, frames, _ = worker(monkeypatch)
    runtime._start.side_effect = module.BrowserStartupError(diagnostic)
    result = await runtime.execute(*MUTATION, AsyncMock())
    assert result["status"] == "failed"
    assert result["error"] == "browser_initialization_failed"
    assert result["message"] == diagnostic
    assert "No page action" in result["hint"]
    assert frames == []


@pytest.mark.asyncio
async def test_authorization_exception_closes_worker(monkeypatch):
    runtime, _, frames, _ = worker(monkeypatch, authorize=AsyncMock(side_effect=ConnectionError()))
    assert (await runtime.execute(*MUTATION, AsyncMock()))["status"] == "failed"
    runtime._stop.assert_awaited_once()
    assert not frames


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,arguments,expected", [
    (*MUTATION, "unknown"), ("browser.tab-list", {}, "failed"),
])
async def test_disconnect_after_dispatch_reports_possible_effects(monkeypatch, operation, arguments, expected):
    runtime, _, _, _ = worker(monkeypatch, on_write=lambda reader, _: reader.feed_eof())
    assert (await runtime.execute(operation, arguments, AsyncMock()))["status"] == expected
    runtime._stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_progress_cannot_starve_permission_renewal(monkeypatch):
    monkeypatch.setattr(module, "AUTHORIZATION_INTERVAL_SECONDS", 0.001)
    authorize = AsyncMock(side_effect=[MATERIAL, {"error": "permission_denied"}])

    def progressing(reader, frame):
        for _ in range(20):
            reader.feed_data(json.dumps({"id": frame["id"], "progress": {"status": "running"}}).encode() + b"\n")
        reply(reader, frame)

    runtime, _, _, _ = worker(monkeypatch, authorize, progressing)

    async def emit(_):
        await asyncio.sleep(0.005)

    result = await asyncio.wait_for(runtime.execute(*MUTATION, emit), timeout=1)
    assert result["status"] == "unknown"
    assert authorize.await_count == 2
    runtime._stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_lease_change_interrupts_pending_command(monkeypatch):
    monkeypatch.setattr(module, "AUTHORIZATION_INTERVAL_SECONDS", 0.001)
    authorize = AsyncMock(side_effect=[MATERIAL, {**MATERIAL, "fence": ["session", 2]}])
    runtime, _, _, _ = worker(monkeypatch, authorize)
    result = await asyncio.wait_for(runtime.execute(*MUTATION, AsyncMock()), timeout=1)
    assert result["status"] == "unknown"
    runtime._stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_command_emits_heartbeat_without_total_deadline(monkeypatch):
    monkeypatch.setattr(module, "AUTHORIZATION_INTERVAL_SECONDS", 0.001)
    runtime, reader, frames, _ = worker(monkeypatch)
    received = []

    async def emit(event):
        received.append(event)
        if len(received) == 3:
            reply(reader, frames[0])

    result = await asyncio.wait_for(runtime.execute(*MUTATION, emit), timeout=1)
    assert result["status"] == "succeeded"
    assert received == [{"_transport": "heartbeat"}] * 3
    assert runtime.authorize.await_count == 4


@pytest.mark.asyncio
async def test_cancellation_stops_worker(monkeypatch):
    runtime, _, frames, _ = worker(monkeypatch)
    task = asyncio.create_task(runtime.execute(*MUTATION, AsyncMock()))
    while not frames:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runtime._stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_lease_restarts_worker_and_closed_turn_never_dispatches(monkeypatch):
    runtime, _, frames, _ = worker(monkeypatch, on_write=reply)
    await runtime.execute(*MUTATION, AsyncMock())
    runtime.authorize.return_value = {**MATERIAL, "fence": ["session", 2]}
    await runtime.execute(*MUTATION, AsyncMock())
    assert runtime._start.await_count == 2
    assert runtime._stop.await_count == 1
    await runtime.close()
    assert (await runtime.execute(*MUTATION, AsyncMock()))["error"] == "runtime_unavailable"
    assert len(frames) == 2


ARTIFACT = {"file": "/data/download.bin", "bytes": 4, "sha256": "a" * 64}


@pytest.mark.asyncio
async def test_download_success_waits_for_exact_durable_receipt(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def commit(operation, arguments, artifacts):
        assert operation == MUTATION[0]
        assert arguments["tab_id"] == MUTATION[1]["tab_id"]
        assert artifacts == [ARTIFACT]
        entered.set()
        await release.wait()
        return {"artifacts": [{**ARTIFACT, "persistence": "durable"}]}

    runtime, _, _, _ = worker(monkeypatch, on_write=lambda reader, frame: reply(reader, frame, {
        "status": "succeeded", "result": {**ARTIFACT, "persistence": "sandbox"}, "_artifacts": [ARTIFACT],
    }))
    runtime.commit = commit
    pending = asyncio.create_task(runtime.execute(*MUTATION, AsyncMock()))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert not pending.done()
    release.set()
    result = await pending
    assert result["status"] == "succeeded"
    assert result["result"]["persistence"] == "durable"
    assert "_artifacts" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [None, {}, {"artifacts": []}, {"error": "storage_unavailable"},
                                         {"artifacts": [{**ARTIFACT, "sha256": "b" * 64, "persistence": "durable"}]}])
async def test_missing_failed_or_mismatched_receipt_is_not_download_success(monkeypatch, receipt):
    runtime, _, frames, _ = worker(monkeypatch, on_write=lambda reader, frame: reply(reader, frame, {
        "status": "succeeded", "result": dict(ARTIFACT), "_artifacts": [ARTIFACT],
    }))
    if receipt is not None:
        runtime.commit = AsyncMock(return_value=receipt)
    result = await runtime.execute(*MUTATION, AsyncMock())
    assert result["status"] == "failed"
    assert result["error"] == "artifact_persistence_failed"
    assert result["local_artifacts"] == [ARTIFACT]
    assert "Do not repeat" in result["hint"]
    assert len(frames) == 1
