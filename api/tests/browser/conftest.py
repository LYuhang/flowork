import asyncio
from contextlib import suppress
from pathlib import Path
import shutil
import subprocess
import time

import pytest
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

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
    client = Redis(unix_socket_path=socket, decode_responses=True,
                   retry=Retry(NoBackoff(), 0))
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


@pytest.fixture
async def browser_connections(directory, monkeypatch):
    from vibecanvas_api.browser import cluster_registry

    _, socket = directory
    runtime = cluster_registry.BrowserConnections(Redis(
        unix_socket_path=socket, decode_responses=True, retry=Retry(NoBackoff(), 0)))
    task = asyncio.create_task(runtime.start())
    await task
    monkeypatch.setitem(cluster_registry._runtimes, asyncio.get_running_loop(), task)
    try:
        yield runtime
    finally:
        await runtime.close()
