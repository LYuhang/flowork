from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from vibecanvas_api.services.agent_runtime.codex_app_server import CodexAppServerError
from vibecanvas_api.services.agent_runtime.codex_steering import steer_native_turn
from vibecanvas_api.services.agent_runtime.protocol import RuntimeSteerInput


def update(**changes):
    return RuntimeSteerInput(input_id='input-1', chat_id='chat', turn_id='run', text='Keep the change small', **changes)


async def send(client, **changes):
    context = dict(chat_id='chat', turn_id='run', thread_id='native-thread', native_turn_id='native-turn', stopping=False)
    return await steer_native_turn(client, update(), **{**context, **changes})


@pytest.mark.asyncio
async def test_steer_is_one_guarded_rpc_with_stable_input_id():
    client = AsyncMock()
    client.request.return_value = {'turnId': 'native-turn'}
    assert await send(client) == {'input_id': 'input-1', 'status': 'accepted', 'error': None}
    client.request.assert_awaited_once_with('turn/steer', {
        'threadId': 'native-thread', 'expectedTurnId': 'native-turn',
        'clientUserMessageId': 'input-1',
        'input': [{'type': 'text', 'text': 'Keep the change small'}],
    }, timeout_s=10.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(('changes', 'error'), [
    ({'chat_id': 'other'}, 'turn_mismatch'),
    ({'turn_id': 'other'}, 'turn_mismatch'),
    ({'stopping': True}, 'turn_stopping'),
    ({'thread_id': None}, 'turn_not_ready'),
    ({'native_turn_id': None}, 'turn_not_ready'),
])
async def test_unavailable_or_different_turn_never_receives_update(changes, error):
    client = AsyncMock()
    assert await send(client, **changes) == {'input_id': 'input-1', 'status': 'rejected', 'error': error}
    client.request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(('code', 'status'), [
    ('codex_app_server_request_failed', 'rejected'),
    ('codex_app_server_request_timed_out', 'unknown'),
    ('codex_app_server_disconnected', 'unknown'),
    ('codex_app_server_invalid_response', 'unknown'),
])
async def test_native_failure_is_not_retried_or_started_as_new_turn(code, status):
    client = AsyncMock()
    client.request.side_effect = CodexAppServerError(code)
    assert await send(client) == {'input_id': 'input-1', 'status': status, 'error': code}
    client.request.assert_awaited_once()
    assert client.request.call_args.args[0] == 'turn/steer'


@pytest.mark.asyncio
async def test_connection_failure_and_wrong_acknowledgement_do_not_claim_acceptance():
    client = AsyncMock()
    client.request.side_effect = BrokenPipeError()
    assert (await send(client))['status'] == 'unknown'
    client.request.assert_awaited_once()
    client = AsyncMock()
    client.request.return_value = {'turnId': 'a-new-turn'}
    assert (await send(client))['error'] == 'steer_response_turn_mismatch'
    client.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed_as_a_retryable_failure():
    client = AsyncMock()
    entered = asyncio.Event()

    async def waiting(*_args, **_kwargs):
        entered.set()
        await asyncio.Future()

    client.request.side_effect = waiting
    task = asyncio.create_task(send(client))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    client.request.assert_awaited_once()


@pytest.mark.parametrize('extra', [{'model': 'other'}, {'approvalPolicy': 'never'}, {'text': '  '}, {'text': 'x' * 32769}])
def test_steering_does_not_accept_turn_configuration_or_empty_text(extra):
    with pytest.raises(ValidationError):
        RuntimeSteerInput.model_validate({
            'input_id': 'input', 'chat_id': 'chat', 'turn_id': 'turn', 'text': 'Keep scope', **extra,
        })
