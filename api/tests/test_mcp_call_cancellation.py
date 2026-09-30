import asyncio
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.services.sandbox import manager
from vibecanvas_api.services.sandbox.process_wait import communicate
from vibecanvas_api.routes import workflow_mcp_broker as broker


@pytest.mark.asyncio
async def test_cancel_waits_for_worker_credential_cleanup(monkeypatch, tmp_path):
    started, cleanup_allowed, cleaned = threading.Event(), threading.Event(), threading.Event()
    def probe(**kwargs):
        secret = tmp_path / 'request.json'
        secret.write_text('private fixture')
        started.set()
        try:
            assert kwargs['cancel_event'].wait(5)
            assert cleanup_allowed.wait(5)
        finally:
            secret.unlink()
            cleaned.set()
        return {'status': 'cancelled'}
    monkeypatch.setattr(manager, 'get_sandbox_provider', lambda **kwargs: SimpleNamespace(run_mcp_probe=probe))
    owner = SimpleNamespace()
    task = asyncio.create_task(manager.SandboxManager.run_mcp_probe(owner, 'tenant', {}, timeout=30, allow_hosts=[]))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        assert not task.done()
        cleanup_allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert cleaned.is_set() and not (tmp_path / 'request.json').exists()
        assert owner._mcp_probe_slots._value == 4
    finally:
        cleanup_allowed.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_cancel_interrupts_wait_on_actual_process():
    event = threading.Event()
    proc = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timer = threading.Timer(0.05, event.set)
    timer.start()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            communicate(proc, timeout=30, cancel_event=event)
    finally:
        timer.cancel()
        proc.kill()
        proc.communicate()


@pytest.mark.asyncio
async def test_watchdog_cancels_owner_on_disconnect(monkeypatch):
    # Isolate the timer to verify the live request check, not elapsed wall time.
    fake_asyncio = SimpleNamespace(sleep=AsyncMock(), CancelledError=asyncio.CancelledError)
    monkeypatch.setattr(broker, 'asyncio', fake_asyncio)
    owner = SimpleNamespace(cancel=lambda: events.append('cancel'))
    events = []
    await broker.watch_call_authorization(SimpleNamespace(is_disconnected=AsyncMock(return_value=True)), object(), owner)
    assert events == ['cancel']


@pytest.mark.skipif(__import__('os').environ.get('FLOWORK_TEST_SKILL_MOUNT') != '1' or not __import__('shutil').which('bwrap'),
                    reason='explicit native Bubblewrap cancellation check')
def test_native_mcp_process_cancellation_removes_private_directory(tmp_path, monkeypatch):
    import tempfile
    from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
    from vibecanvas_api.services.sandbox import gvisor
    original = tempfile.TemporaryDirectory
    def private_directory(**kwargs):
        return original(dir=tmp_path, **kwargs)
    monkeypatch.setattr(gvisor.tempfile, 'TemporaryDirectory', private_directory)
    event = threading.Event()
    timer = threading.Timer(2, event.set)
    timer.start()
    try:
        result = BubblewrapProvider('/usr/bin/bwrap').run_mcp_probe(
            request={'connection': {'transport': 'stdio', 'command': sys.executable,
                'args': ['-c', 'import time;time.sleep(30)']}, 'timeout_s': 30},
            timeout=30, allow_hosts=set(), cancel_event=event)
        assert result['status'].startswith('error:')
        assert event.is_set()
        assert not list(tmp_path.iterdir())
    finally:
        timer.cancel()
