import asyncio
from contextlib import suppress
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest
from redis.asyncio import Redis

from vibecanvas_api.browser.connection_directory import ConnectionDirectory


@pytest.fixture
async def directory(tmp_path):
    executable = shutil.which("redis-server")
    if not executable:
        pytest.skip("redis-server is required for cross-instance routing tests")
    socket = str(tmp_path / "redis.sock")
    process = subprocess.Popen([executable, "--port", "0", "--unixsocket", socket,
                                "--save", "", "--appendonly", "no"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = Redis(unix_socket_path=socket, decode_responses=True)
    try:
        deadline = time.monotonic() + 5
        while not Path(socket).exists():
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("isolated Redis did not start")
            await asyncio.sleep(0.01)
        await client.ping()
        yield ConnectionDirectory(client), socket
    finally:
        await client.aclose()
        process.terminate()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3)
        if process.poll() is None:
            process.kill()
            process.wait()


async def test_owner_is_visible_to_another_process_and_isolated_by_identity(directory):
    first, socket = directory
    owner = await first.register(instance_id="api-a", transport_id="tenant:user:browser",
                                 session_id="session-a")
    code = """
import asyncio,sys
from redis.asyncio import Redis
from vibecanvas_api.browser.connection_directory import ConnectionDirectory
async def main():
    client=Redis(unix_socket_path=sys.argv[1],decode_responses=True)
    try:
        owner=await ConnectionDirectory(client).find_for_session('tenant','user','session-a')
        print(owner.encode())
    finally: await client.aclose()
asyncio.run(main())
"""
    result = await asyncio.to_thread(subprocess.check_output, [sys.executable, "-c", code, socket], text=True)
    assert json.loads(result)["connection_id"] == owner.connection_id
    assert json.loads(result)["instance_id"] == "api-a"
    assert await first.find_for_session("other-tenant", "user", "session-a") is None
    assert await first.find_for_session("tenant", "other-user", "session-a") is None
    assert await first.find_for_session("tenant", "user", "other-session") is None


async def test_old_connection_cannot_renew_or_delete_replacement(directory):
    first, _ = directory
    old = await first.register(instance_id="api-a", transport_id="tenant:user:browser", session_id="old")
    new = await first.register(instance_id="api-b", transport_id="tenant:user:browser", session_id="new")
    assert not await first.remove(old)
    assert not await first.renew(old)
    assert await first.get(old.transport_id) == new
    assert await first.find_for_session("tenant", "user", "old") is None
    assert await first.find_for_session("tenant", "user", "new") == new
    assert await first.remove(new)
    assert await first.find_for_session("tenant", "user", "new") is None


async def test_ambiguity_does_not_select_a_different_browser_and_cdp_is_separate(directory):
    first, _ = directory
    one = await first.register(instance_id="api-a", transport_id="tenant:user:one", session_id="session")
    two = await first.register(instance_id="api-b", transport_id="tenant:user:two", session_id="session")
    controller = await first.register(instance_id="api-c", transport_id=one.transport_id,
                                      session_id="session", channel="chat:a")
    assert await first.find_for_session("tenant", "user", "session") is None
    assert await first.get(one.transport_id, "chat:a") == controller
    assert await first.get(one.transport_id, "chat:b") is None
    assert await first.remove(two)
    assert await first.find_for_session("tenant", "user", "session") == one
    assert await first.get(one.transport_id) == one


async def test_expired_process_ownership_disappears_without_clean_shutdown(directory):
    first, _ = directory
    first.ttl = 1
    owner = await first.register(instance_id="dead-process", transport_id="tenant:user:browser", session_id="session")
    await asyncio.sleep(1.1)
    assert await first.get(owner.transport_id) is None
    assert await first.find_for_session("tenant", "user", "session") is None
    assert not await first.renew(owner)  # cannot resurrect a lost connection
