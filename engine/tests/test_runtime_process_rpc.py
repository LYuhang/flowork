"""Exercise cancellation and generation fencing through a real runtime process."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import uuid

import pytest

from vibecanvas_engine.sandbox_bus import encode_frame, read_frame
from test_human_approval_runtime import approval_workflow


@pytest.mark.asyncio
async def test_process_rpc_cancel_isolation_ack_release_and_restart(tmp_path):
    socket = tmp_path / 'runtime.sock'
    token = secrets.token_hex(32)
    process = None
    generation = None
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')

    async def rpc(method, args=None, *, fence=True):
        reader, writer = await asyncio.open_unix_connection(str(socket))
        try:
            writer.write(encode_frame({'token': token, 'generation': generation if fence else None,
                'method': method, 'args': args or {}}))
            await writer.drain()
            return await asyncio.wait_for(read_frame(reader), 5)
        finally:
            writer.close()
            await writer.wait_closed()

    async def start():
        nonlocal process
        process = await asyncio.create_subprocess_exec(sys.executable, '-m',
            'vibecanvas_engine.runtime.rpc', str(socket), '2', env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        process.stdin.write((json.dumps({'token': token}) + '\n').encode())
        await process.stdin.drain()
        process.stdin.close()
        async with asyncio.timeout(15):
            while True:
                if process.returncode is not None:
                    raise AssertionError(f'runtime exited: {process.returncode}')
                try:
                    hello = await rpc('hello', fence=False)
                    return hello['result']['generation']
                except (FileNotFoundError, ConnectionRefusedError):
                    await asyncio.sleep(0.02)

    async def status(run, expected):
        async with asyncio.timeout(5):
            while True:
                response = await rpc('status', {'invocation_id': run})
                assert response['ok'], response
                if response['result']['status'] == expected:
                    return response['result']
                await asyncio.sleep(0.01)

    try:
        generation = await start()
        graph = approval_workflow()
        graph['node_2']['node_config']['timeout_seconds'] = 60
        assert (await rpc('install', {'revision': 'qa', 'workflow': graph}))['ok']
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        for run in (first, second):
            assert (await rpc('invoke', {'invocation_id': run, 'revision': 'qa', 'inputs': {}}))['ok']
            await status(run, 'waiting_approval')
        assert (await rpc('cancel', {'invocation_id': first}))['ok']
        await status(first, 'cancelled')
        survivor = await status(second, 'waiting_approval')
        assert (await rpc('decide', {'invocation_id': second,
            'approval_id': survivor['approvals'][0]['approval_id'], 'approved': True}))['ok']
        await status(second, 'succeeded')
        await status(first, 'cancelled')
        for run, expected in ((first, 'cancelled'), (second, 'succeeded')):
            events = (await rpc('events', {'invocation_id': run}))['result']
            assert events['events'][-1]['type'] == 'result'
            # Lose the ACK response. Completed execution state is released;
            # the host already persisted the business result before this ACK.
            _, ack_writer = await asyncio.open_unix_connection(str(socket))
            ack_writer.write(encode_frame({'token': token, 'generation': generation,
                'method': 'acknowledge', 'args': {'invocation_id': run, 'through': events['seq']}}))
            await ack_writer.drain()
            ack_writer.close()
            await ack_writer.wait_closed()
            async with asyncio.timeout(5):
                while (await rpc('status', {'invocation_id': run})).get('ok'):
                    await asyncio.sleep(0.01)
            assert await rpc('status', {'invocation_id': run}) == {'ok': False, 'error': 'not_found'}
        waiting = str(uuid.uuid4())
        assert (await rpc('invoke', {'invocation_id': waiting, 'revision': 'qa', 'inputs': {}}))['ok']
        await status(waiting, 'waiting_approval')
        process.kill()
        await asyncio.wait_for(process.wait(), 5)
        new_generation = await start()
        assert new_generation != generation
        stale = await rpc('invoke', {'invocation_id': waiting, 'revision': 'qa', 'inputs': {}})
        assert stale == {'ok': False, 'error': 'execution_lost'}
        generation = new_generation
        assert await rpc('status', {'invocation_id': waiting}) == {'ok': False, 'error': 'not_found'}
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 5)
