from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from vibecanvas_api.services.agent_runtime.goal_commands import GoalCommand, parse_goal_command
from vibecanvas_api.services.agent_runtime.codex_goal import apply_goal_command, pause_active_goal


def test_commands_are_explicit_and_do_not_capture_regular_messages():
    assert parse_goal_command('/goal Finish all ten rounds').objective == 'Finish all ten rounds'
    assert parse_goal_command('/goal:resume').action == 'resume'
    assert parse_goal_command('/goal').action == 'status'
    assert parse_goal_command('/goal:status').action == 'status'
    assert parse_goal_command('/goal:clear').action == 'clear'
    assert parse_goal_command('Explain /goal:resume') is None
    assert parse_goal_command('/goals something') is None


@pytest.mark.parametrize('text', ['/goal:new', '/goal:new Finish', '/goal:resume new goal', '/goal:bogus', '/goal ' + 'x'*4001])
def test_invalid_controls_fail_before_runtime(text):
    with pytest.raises(ValueError):
        parse_goal_command(text)


@pytest.mark.asyncio
async def test_resume_preserves_objective_and_usage():
    client = AsyncMock()
    client.request.side_effect = [{'goal': {'status': 'paused', 'objective': 'original', 'tokensUsed': 123}}, {'goal': {'status': 'active', 'tokensUsed': 123}}]
    result = await apply_goal_command(client, 'thread', GoalCommand(action='resume'))
    assert result['tokensUsed'] == 123
    client.request.assert_called_with('thread/goal/set', {'threadId': 'thread', 'status': 'active'}, timeout_s=10.0)


@pytest.mark.asyncio
@pytest.mark.parametrize('goal', [None, {'status': 'complete'}])
async def test_resume_cannot_create_or_restart_completed_goal(goal):
    client = AsyncMock(); client.request.return_value = {'goal': goal}
    with pytest.raises(ValueError):
        await apply_goal_command(client, 'thread', GoalCommand(action='resume'))
    assert client.request.call_count == 1


@pytest.mark.asyncio
async def test_new_goal_does_not_invent_a_budget():
    client = AsyncMock(); client.request.return_value = {'goal': {'status': 'active'}}
    await apply_goal_command(client, 'thread', GoalCommand(action='new', objective='Finish'))
    client.request.assert_called_once_with('thread/goal/set', {'threadId': 'thread', 'status': 'active', 'objective': 'Finish'}, timeout_s=10.0)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['active', 'complete', 'blocked', 'paused', 'usageLimited', 'budgetLimited'])
async def test_stop_only_pauses_active_goal(status):
    client = AsyncMock(); client.request.return_value = {'goal': {'status': status}}
    await pause_active_goal(client, 'thread')
    assert client.request.call_count == (2 if status == 'active' else 1)
    if status == 'active':
        client.request.assert_called_with('thread/goal/set', {'threadId': 'thread', 'status': 'paused'}, timeout_s=10.0)


@pytest.mark.asyncio
async def test_goal_survives_native_thread_recreation_with_remaining_budget():
    from vibecanvas_api.services.agent_runtime.codex_goal import GoalContinuity
    saved = {'threadId': 'old', 'objective': 'Finish', 'status': 'paused',
             'timeUsedSeconds': 75, 'tokensUsed': 100, 'tokenBudget': 1000, 'createdAt': 123}
    client = AsyncMock()
    client.request.return_value = {'goal': {'threadId': 'new', 'objective': 'Finish',
        'status': 'paused', 'timeUsedSeconds': 0, 'tokensUsed': 0, 'tokenBudget': 900}}
    continuity = GoalContinuity(saved)
    native = await continuity.restore(client, 'new', None)
    client.request.assert_called_once_with('thread/goal/set', {'threadId': 'new',
        'objective': 'Finish', 'status': 'paused', 'tokenBudget': 900}, timeout_s=10.0)
    projected = continuity.project(native)
    assert projected['timeUsedSeconds'] == 75
    assert projected['tokensUsed'] == 100
    assert projected['tokenBudget'] == 1000
    assert projected['createdAt'] == 123
    next_request = GoalContinuity(projected)
    await next_request.restore(client, 'new', native)
    assert next_request.project({**native, 'tokensUsed': 10})['tokensUsed'] == 110
    assert client.request.call_count == 1


@pytest.mark.asyncio
async def test_goal_without_durable_snapshot_is_not_recreated():
    from vibecanvas_api.services.agent_runtime.codex_goal import GoalContinuity
    client = AsyncMock()
    assert await GoalContinuity(None).restore(client, 'new', None) is None
    client.request.assert_not_called()
