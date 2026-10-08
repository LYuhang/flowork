"""Real process boundary checks; full HTTP/WebSocket acceptance is separate."""
import asyncio
import json
import sys
from unittest.mock import AsyncMock

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from vibecanvas_api.browser.cluster_registry import BrowserConnections


async def test_commands_and_results_cross_processes_without_replay(directory):
    _, socket = directory
    child_code = '''
import asyncio, json, sys
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from vibecanvas_api.browser.cluster_registry import BrowserConnections

async def main():
    runtime = await BrowserConnections(Redis(unix_socket_path=sys.argv[1],
        decode_responses=True, retry=Retry(NoBackoff(), 0))).start()
    calls = 0
    async def send(payload):
        nonlocal calls
        calls += 1
        await runtime.send('tenant:user:browser',
            {'id': payload['id'], 'result': payload['method'], 'calls': calls}, 'chat:test')
    async def close():
        pass
    try:
        await runtime.bind('tenant:user:browser', send, close, 'session')
        print(json.dumps({'instance': runtime.instance_id}), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await runtime.close()
asyncio.run(main())
'''
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-c', child_code, socket,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    runtime = await BrowserConnections(Redis(unix_socket_path=socket,
        decode_responses=True, retry=Retry(NoBackoff(), 0))).start()
    replies = asyncio.Queue()
    try:
        ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert ready['instance'] != runtime.instance_id
        await runtime.bind('tenant:user:browser', replies.put, AsyncMock(), '', 'chat:test')
        owner = await runtime.directory.find_for_session('tenant', 'user', 'session')
        assert owner.instance_id == ready['instance']
        for index in range(3):
            assert await runtime.send(owner.transport_id, {'id': index, 'method': 'snapshot'})
            assert await asyncio.wait_for(replies.get(), 2) == {
                'id': index, 'result': 'snapshot', 'calls': index + 1}
        assert replies.empty()
        process.stdin.write(b'close\n')
        await process.stdin.drain()
        await asyncio.wait_for(process.wait(), 5)
        assert process.returncode == 0, (await process.stderr.read()).decode()
        assert not await runtime.send(owner.transport_id, {'id': 4, 'method': 'click'})
        assert replies.empty()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await runtime.close()
