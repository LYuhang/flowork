from types import SimpleNamespace
import uuid

import pytest

from vibecanvas_api.services.sandbox.workflow_rpc_slot import WorkflowInvocationSlot


@pytest.mark.asyncio
async def test_invoke_propagates_timeout_and_close_releases_ownership(tmp_path):
    calls = []
    fail_once = True

    class Client:
        async def call(self, method, **args):
            nonlocal fail_once
            calls.append((method, args))
            if method == 'invoke' and fail_once:
                fail_once = False
                raise TimeoutError('acceptance reply lost')
            return {'status': 'succeeded', 'seq': 1}

    client = Client()
    worker = SimpleNamespace(root=tmp_path, artifacts=tmp_path, workflow={}, revision='test',
                             alive=True, client=client)
    slot = WorkflowInvocationSlot(worker)
    slot.client = client
    run = str(uuid.uuid4())
    with pytest.raises(TimeoutError):
        await slot.invoke(run, {}, {})
    sent = [args for method, args in calls if method == 'invoke']
    assert len(sent) == 1 and sent[0]['invocation_id'] == run
    assert 'slot_id' not in sent[0] and 'slot_sequence' not in sent[0]
    assert slot.invocation_id == run
    await slot.close()
    assert slot.invocation_id is None
    assert [method for method, _ in calls] == ['invoke', 'cancel', 'acknowledge']
