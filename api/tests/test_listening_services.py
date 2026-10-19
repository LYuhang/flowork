import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from vibecanvas_api.services.sandbox.listening_services import listening_services, stop_service
from vibecanvas_api.services.sandbox.manager import SandboxManager, SandboxSession


@pytest.mark.asyncio
async def test_real_listener_lifecycle_and_stale_stop():
    chat = '10000000-0000-0000-0000-000000000001'
    script = "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); print(s.getsockname()[1],flush=True); time.sleep(60)"
    child = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                             env={**os.environ, 'FLOWORK_SERVICE_CHAT_ID': chat})
    unrelated = socket.socket()
    unrelated.bind(('127.0.0.1', 0))
    unrelated.listen()
    try:
        port = int(child.stdout.readline())
        owners = [s for s in listening_services() if s['port'] == port]
        assert owners == [{'port': port, 'pid': child.pid,
                           'start': Path(f'/proc/{child.pid}/stat').read_text().rsplit(')', 1)[1].split()[19],
                           'chat_id': chat}]
        assert unrelated.getsockname()[1] not in [s['port'] for s in listening_services()]
        with pytest.raises(RuntimeError, match='service_changed'):
            await stop_service(port, [{**owners[0], 'start': 'old'}])
        assert child.poll() is None
        assert not any(s['port'] == port for s in await stop_service(port, owners))
        assert child.wait(timeout=2) == -15
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=2)
        unrelated.close()


@pytest.mark.asyncio
async def test_last_service_and_last_agent_start_idle_clock():
    session = SandboxSession(tenant_id='tenant', wf_id='project', run_dir=None,
                             overlay_dir=None, provider=MagicMock(), base_binds=[], expose_run=False)
    manager = SandboxManager(max_resident=2, idle_ttl_s=10)
    manager._sessions[('tenant', 'project')] = session
    session.service_inventory = AsyncMock()
    session._listening_services = [{'chat_id': 'a', 'port': 8080}, {'chat_id': 'b', 'port': 8081}]
    session.last_used = 0
    assert await manager.sweep_idle() == 0
    status = await manager.status('tenant', 'project')
    assert status['ttl_paused']
    assert status['serving_chat_ids'] == ['a', 'b']
    session._listening_services.pop()
    assert (await manager.status('tenant', 'project'))['serving_chat_ids'] == ['a']
    session._begin_activity()
    session._listening_services.clear()
    assert (await manager.status('tenant', 'project'))['ttl_paused']
    session._end_activity()
    status = await manager.status('tenant', 'project')
    assert not status['ttl_paused']
    assert status['idle_elapsed_s'] < 1
    assert status['serving_chat_ids'] == []


@pytest.mark.asyncio
async def test_stop_wait_does_not_block_runtime_turn_messages(monkeypatch):
    from vibecanvas_api.services.agent_runtime import sandbox_entry as entry
    from vibecanvas_api.services.sandbox import listening_services as services
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    class Channel:
        recv = incoming.get
        send = outgoing.put
    blocked = asyncio.Event()
    async def stop(*_):
        await blocked.wait()
        return []
    monkeypatch.setattr(services, 'stop_service', stop)
    monkeypatch.setattr(services, 'listening_services', lambda: [])
    task = asyncio.create_task(entry.serve_turns(Channel()))
    try:
        await incoming.put({'type': 'service_inventory', 'action': 'stop', 'port': 8080, 'expected': [], 'turn_id': 'stop'})
        await incoming.put({'type': 'service_inventory', 'turn_id': 'list'})
        assert (await asyncio.wait_for(outgoing.get(), 1))['turn_id'] == 'list'
        blocked.set()
        assert (await asyncio.wait_for(outgoing.get(), 1))['turn_id'] == 'stop'
    finally:
        await incoming.put(None)
        await task


def test_bind_mounted_proc_reports_namespace_pid(tmp_path):
    (tmp_path / 'net').mkdir()
    (tmp_path / 'net/tcp').write_text('header\n0: 0100007F:1F90 0 0A 0 0 0 0 0 12345\n')
    (tmp_path / 'net/tcp6').write_text('header\n')
    (tmp_path / 'self/ns').mkdir(parents=True)
    (tmp_path / 'self/ns/pid').symlink_to('pid:[1]')
    proc = tmp_path / '9999'
    (proc / 'ns').mkdir(parents=True)
    (proc / 'ns/pid').symlink_to('pid:[1]')
    (proc / 'status').write_text('NSpid:\t9999\t7\n')
    fields = ['S', '1'] + ['0'] * 17 + ['456']
    (proc / 'stat').write_text('9999 (server) ' + ' '.join(fields))
    (proc / 'environ').write_bytes(b'FLOWORK_SERVICE_CHAT_ID=chat-a\0')
    (proc / 'fd').mkdir()
    (proc / 'fd/3').symlink_to('socket:[12345]')
    assert listening_services(tmp_path) == [{'port': 8080, 'pid': 7, 'start': '456', 'chat_id': 'chat-a'}]
