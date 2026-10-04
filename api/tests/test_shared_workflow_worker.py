"""Concurrent real sandbox calls share a process, never their cancellation."""
import asyncio
import shutil
from types import SimpleNamespace
import uuid

import pytest

from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
from vibecanvas_api.services.sandbox.workflow_rpc_pool import WorkflowRpcPool, WorkflowPoolFull


def code_graph():
    def node(number, name, kind, inputs, outputs, config, children):
        return dict(node_id=f"node_{number}", node_name=name, node_type=kind,
                    node_description=name, input_fields=inputs, output_fields=outputs,
                    node_config=config, children=children)
    fields = {"label": {"type": "string", "value": "", "reference": ""},
              "delay": {"type": "number", "value": 0, "reference": ""}}
    outputs = {key: {"type": f["type"], "description": key} for key, f in fields.items()}
    return {
        "node_1": node(1, "__start__", "StartNode", fields, outputs, {}, ["node_2"]),
        "node_2": node(2, "work", "CodeNode",
            {key: {**f, "reference": f"__start__.{key}"} for key, f in fields.items()},
            {"label": {"type": "string", "description": "label"}},
            {"programming_language": "python", "process_fn":
             "def process_fn(inputs):\n"
             "    import time\n"
             "    with open('/run/' + inputs['label'] + '.started', 'w') as f:\n"
             "        f.write(inputs['label'])\n"
             "    time.sleep(inputs['delay'])\n"
             "    with open('/run/' + inputs['label'] + '.finished', 'w') as f:\n"
             "        f.write(inputs['label'])\n"
             "    return {'label': inputs['label']}"}, ["node_3"]),
        "node_3": node(3, "__end__", "EndNode",
            {"label": {"type": "string", "value": "", "reference": "work.label"}},
            {"label": {"type": "string", "description": "label"}}, {}, []),
    }


async def terminal(slot, execution):
    async with asyncio.timeout(10):
        while True:
            state = await slot.client.call("events", invocation_id=execution, wait_seconds=.1)
            if state["status"] in {"succeeded", "failed", "cancelled", "timed_out"}:
                return state


@pytest.mark.asyncio
async def test_shared_worker_cancel_capacity_files_and_recovery():
    if not shutil.which("bwrap"):
        pytest.skip("requires bubblewrap")
    session = SimpleNamespace(provider=BubblewrapProvider(shutil.which("bwrap")),
                              workspace_folders=(), _rw_binds=[], skills_dir=None)
    pool = WorkflowRpcPool.for_session(session=session, revision="v1", workflow=code_graph(), capacity=2)
    first, second, third = (str(uuid.uuid4()) for _ in range(3))
    try:
        await pool.prewarm()
        async with pool.acquire(first) as a, pool.acquire(second) as b:
            assert a.handle.proc.pid == b.handle.proc.pid
            pid, generation = a.handle.proc.pid, a.client.generation
            assert (await a.client.call("hello"))["capacity"] == 2
            assert a.root == b.root
            root = a.artifacts
            (root / "shared.txt").write_text("belongs to the sandbox")
            await a.invoke(first, {"label": "slow", "delay": 30}, {})
            await b.invoke(second, {"label": "sibling", "delay": .5}, {})
            with pytest.raises(WorkflowPoolFull):
                async with pool.acquire(third):
                    pytest.fail("full capacity must reject immediately")
            async with asyncio.timeout(10):
                while not (root / "slow.started").exists():
                    await asyncio.sleep(.01)
            await a.close()
            assert a.invocation_id is None
            assert a.alive and b.alive and a.handle.proc.pid == pid
            assert not (root / "slow.finished").exists()
            state = await terminal(b, second)
            assert state["status"] == "succeeded", state
            assert state["events"][-1]["final_outputs"]["__end__"] == {"label": "sibling"}
            await b.release(second, state["seq"])
            assert (root / "shared.txt").read_text() == "belongs to the sandbox"
            assert (root / "sibling.finished").read_text() == "sibling"
        assert not pool.busy
        async with pool.acquire(third) as c:
            assert c.client.generation == generation and c.handle.proc.pid == pid
            await c.invoke(third, {"label": "next", "delay": 0}, {})
            state = await terminal(c, third)
            assert state["status"] == "succeeded", state
            await c.release(third, state["seq"])
        # A real process crash loses all owners, but subsequent calls recover
        # without replacing the sandbox's mounted files.
        await pool._workers[0].close()
        assert pool.accepting and not pool.ready
        async with pool.acquire(str(uuid.uuid4())) as replacement:
            assert replacement.client.generation != generation
            assert (root / "shared.txt").exists()
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_multiworker_balances_reservations_and_shares_run(tmp_path):
    if not shutil.which('bwrap'):
        pytest.skip('requires bubblewrap')
    session = SimpleNamespace(provider=BubblewrapProvider(shutil.which('bwrap')),
                              workspace_folders=(), _rw_binds=[], skills_dir=None)
    pool = WorkflowRpcPool.for_session(session=session, revision='v1', workflow=code_graph(),
                                       capacity=4, worker_count=2, artifacts_root=str(tmp_path))
    try:
        await pool.prewarm()
        async with pool.acquire('a') as a, pool.acquire('b') as b, pool.acquire('c') as c:
            assert a.handle.proc.pid != b.handle.proc.pid
            assert c.handle.proc.pid == a.handle.proc.pid
            assert a.artifacts == b.artifacts == tmp_path
            execution = str(uuid.uuid4())
            await b.invoke(execution, {'label': 'cross-worker', 'delay': 0}, {})
            state = await terminal(b, execution)
            assert state['status'] == 'succeeded'
            await b.release(execution, state['seq'])
            assert (a.artifacts / 'cross-worker.finished').read_text() == 'cross-worker'
        # A replacement revision may mount the same directory; closing workers
        # must not remove the external, deployment-owned projection.
        await pool.close()
        assert (tmp_path / 'cross-worker.finished').exists()
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_unlimited_pool_reserves_more_than_default_four_slots(tmp_path):
    from contextlib import AsyncExitStack
    if not shutil.which('bwrap'):
        pytest.skip('requires bubblewrap')
    session = SimpleNamespace(provider=BubblewrapProvider(shutil.which('bwrap')),
                              workspace_folders=(), _rw_binds=[], skills_dir=None)
    pool = WorkflowRpcPool.for_session(session=session, revision='unlimited', workflow=code_graph(),
                                       capacity=-1, worker_count=2, artifacts_root=str(tmp_path))
    try:
        async with AsyncExitStack() as stack:
            slots = [await stack.enter_async_context(pool.acquire(str(uuid.uuid4()))) for _ in range(10)]
            assert len(pool._owners) == 10
            assert len({slot.handle.proc.pid for slot in slots}) == 2
            assert (await slots[0].client.call('hello'))['capacity'] == -1
            assert sum(slot.worker is pool._workers[0] for slot in slots) == 5
        assert not pool.busy
        # Removing one reservation changes the next routing decision. Least
        # occupied wins even when the tie-break cursor points to the busy worker.
        async with pool.acquire('counter-a') as a:
            async with pool.acquire('counter-b'):
                retained = pool.acquire('counter-c')
                c = await retained.__aenter__()
                assert c.worker is a.worker
            try:
                async with pool.acquire('counter-d') as d:
                    assert d.worker is not a.worker
                    async with pool.acquire('counter-e') as e:
                        assert e.worker is d.worker
                        assert len(pool._owners) == 4
            finally:
                await retained.__aexit__(None, None, None)
        assert not pool.busy
    finally:
        await pool.close()
