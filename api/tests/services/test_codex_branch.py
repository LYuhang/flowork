from unittest.mock import AsyncMock

import pytest

from vibecanvas_api.services.agent_runtime.codex_branch import fork_native_thread


@pytest.mark.asyncio
async def test_branch_sets_inclusive_boundary_defers_goal_and_clears_only_new_goal():
    client = AsyncMock()
    client.request.side_effect = [{'thread': {'id': 'new-thread'}}, {}]
    assert await fork_native_thread(client, source_thread_id='source', last_turn_id='last',
                                    overrides={'cwd': '/data', 'threadId': 'ignored'}) == 'new-thread'
    assert client.request.await_count == 2
    assert client.request.await_args_list[0].args == ('thread/fork', {
        'cwd': '/data', 'threadId': 'source', 'lastTurnId': 'last',
        'excludeTurns': True, 'deferGoalContinuation': True,
    })
    assert client.request.await_args_list[1].args == ('thread/goal/clear', {'threadId': 'new-thread'})


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [{}, {'thread': {'id': 'source'}}, {'thread': {'id': ''}}])
async def test_invalid_fork_response_never_clears_the_source_goal(response):
    client = AsyncMock()
    client.request.return_value = response
    with pytest.raises(RuntimeError, match='native_branch_invalid_thread'):
        await fork_native_thread(client, source_thread_id='source', last_turn_id='last', overrides={})
    client.request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_at', ['fork', 'clear'])
async def test_fork_or_clear_failure_is_reported_without_repeating_the_fork(failure_at):
    client = AsyncMock()
    client.request.side_effect = [ConnectionError('lost')] if failure_at == 'fork' else [
        {'thread': {'id': 'new-thread'}}, ConnectionError('lost'),
    ]
    with pytest.raises(ConnectionError):
        await fork_native_thread(client, source_thread_id='source', last_turn_id='last', overrides={})
    assert [call.args[0] for call in client.request.await_args_list].count('thread/fork') == 1
    assert not any(call.args[0] == 'turn/start' for call in client.request.await_args_list)
