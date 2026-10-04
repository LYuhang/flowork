"""Node-local transports must close before the worker's event loop ends."""
import asyncio
from unittest.mock import patch

import httpx
import pytest

from vibecanvas_api.services.workflow_subagent import workflow_chat_model


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['openai', 'azure_openai'])
@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancel', 'init_failure'])
async def test_model_transports_close_on_every_exit(provider, outcome):
    clients = []

    def init(model, **kwargs):
        clients.extend([kwargs['http_client'], kwargs['http_async_client']])
        assert all(not client.is_closed for client in clients)
        if outcome == 'init_failure':
            raise ValueError('invalid model')
        return object()

    async def run():
        async with workflow_chat_model({'model': provider + ':test', 'api_key': 'test'}) as model:
            assert model is not None
            if outcome == 'failure':
                raise RuntimeError('tool failed')
            if outcome == 'cancel':
                asyncio.current_task().cancel()
                await asyncio.sleep(0)

    with patch('langchain.chat_models.init_chat_model', init):
        if outcome == 'success':
            await run()
        else:
            error = {'failure': RuntimeError, 'cancel': asyncio.CancelledError,
                     'init_failure': ValueError}[outcome]
            with pytest.raises(error):
                await asyncio.create_task(run())
    assert len(clients) == 2
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_partial_transport_construction_closes_sync_client():
    client = httpx.Client()
    with (patch('vibecanvas_api.services.workflow_subagent.httpx.Client', return_value=client),
          patch('vibecanvas_api.services.workflow_subagent.httpx.AsyncClient',
                side_effect=RuntimeError('transport construction failed')),
          pytest.raises(RuntimeError, match='transport construction failed')):
        async with workflow_chat_model({'model': 'openai:test', 'api_key': 'test'}):
            pytest.fail('construction must fail')
    assert client.is_closed


@pytest.mark.asyncio
async def test_real_openai_adapter_uses_owned_transports():
    async with workflow_chat_model({
        'model': 'openai:test-model', 'api_key': 'test-capability',
        'base_url': 'https://broker.example.invalid/v1',
    }) as model:
        assert not model.root_client.is_closed()
        assert not model.root_async_client.is_closed()
        assert model.root_client._client is model.http_client
        assert model.root_async_client._client is model.http_async_client
    assert model.root_client.is_closed()
    assert model.root_async_client.is_closed()
