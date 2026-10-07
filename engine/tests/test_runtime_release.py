"""Completed execution memory is released after the host acknowledges it."""
import uuid

import pytest

from vibecanvas_engine.runtime.executions import WorkflowRuntime
from test_human_approval_runtime import approval_workflow


@pytest.mark.asyncio
async def test_acknowledged_executions_leave_no_history_in_worker():
    runtime = WorkflowRuntime(capacity=2)
    graph = approval_workflow()
    del graph['node_2']
    graph['node_1']['children'] = ['node_3']
    graph['node_3']['input_fields'] = {}
    graph['node_3']['output_fields'] = {}
    runtime.install('small', graph)
    try:
        for _ in range(200):
            run = str(uuid.uuid4())
            runtime.invoke(run, 'small', {})
            await runtime.executions[run].task
            state = runtime.status(run)
            assert state['status'] == 'succeeded'
            runtime.acknowledge(run, state['seq'])
            assert not runtime.executions
            with pytest.raises(KeyError):
                runtime.status(run)
        assert not hasattr(runtime, 'completed')
        assert not hasattr(runtime, 'slots')
    finally:
        await runtime.close()
