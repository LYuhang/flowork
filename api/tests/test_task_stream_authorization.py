import uuid
import pytest
from vibecanvas_api.services.sse_bridge import task_event_stream


@pytest.mark.asyncio
async def test_task_stream_closes_before_replay_when_lease_is_revoked():
    calls = 0

    async def denied() -> bool:
        nonlocal calls
        calls += 1
        return False

    stream = task_event_stream(
        task_id=uuid.uuid4(),
        last_event_id=0,
        tenant_id=str(uuid.uuid4()),
        authorization_guard=denied,
    )
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert calls == 1
