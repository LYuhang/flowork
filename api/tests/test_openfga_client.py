from __future__ import annotations

import json

import httpx
import pytest

from vibecanvas_api.authorization.openfga_client import (
    OpenFgaHttpClient,
    OpenFgaTuple,
    OpenFgaUnavailableError,
)
from vibecanvas_api.authorization.types import ConsistencyPreference


def _client(handler) -> OpenFgaHttpClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="https://openfga.test",
        transport=transport,
    )
    return OpenFgaHttpClient(
        api_url="https://openfga.test",
        store_id="store-1",
        authorization_model_id="model-1",
        client=http,
    )


@pytest.mark.asyncio
async def test_batch_check_preserves_order_and_pins_model():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "result": {
                    item["correlation_id"]: {
                        "allowed": int(item["correlation_id"]) % 2 == 0
                    }
                    for item in payload["checks"]
                }
            },
        )

    client = _client(handler)
    checks = [
        ("user:alice", "can_view", f"workflow:wf-{index}")
        for index in range(55)
    ]
    result = await client.batch_check(
        checks,
        consistency=ConsistencyPreference.HIGHER_CONSISTENCY,
    )
    assert result == tuple(index % 2 == 0 for index in range(55))
    assert [len(item["checks"]) for item in requests] == [50, 5]
    assert {
        item["authorization_model_id"] for item in requests
    } == {"model-1"}
    assert {
        item["consistency"] for item in requests
    } == {"HIGHER_CONSISTENCY"}


@pytest.mark.asyncio
async def test_per_check_error_fails_closed():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": {"0": {"error": {"internal_error": "internal_error"}}}},
        )

    with pytest.raises(
        OpenFgaUnavailableError,
        match="authorization_check_failed",
    ):
        await _client(handler).batch_check(
            [("user:alice", "can_view", "workflow:wf")],
            consistency=ConsistencyPreference.MINIMIZE_LATENCY,
        )


@pytest.mark.asyncio
async def test_transport_and_invalid_responses_fail_closed_without_body():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"message": "contains-sensitive-tuple"},
        )

    with pytest.raises(OpenFgaUnavailableError) as exc_info:
        await _client(handler).check(
            user="user:alice",
            relation="can_view",
            object_="workflow:wf",
            consistency=ConsistencyPreference.MINIMIZE_LATENCY,
        )
    assert "contains-sensitive-tuple" not in str(exc_info.value)
    assert exc_info.value.reason_code == "authorization_unavailable"


@pytest.mark.asyncio
async def test_read_and_write_use_exact_tuple_contract():
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append((request.url.path, payload))
        if request.url.path.endswith("/read"):
            return httpx.Response(
                200,
                json={
                    "tuples": [
                        {
                            "key": {
                                "user": "user:alice",
                                "relation": "manager",
                                "object": "workflow:wf",
                            }
                        }
                    ],
                    "continuation_token": "",
                },
            )
        return httpx.Response(200, json={})

    client = _client(handler)
    tuple_ = OpenFgaTuple("user:alice", "manager", "workflow:wf")
    page = await client.read(tuple_key=tuple_)
    assert page.tuples == (tuple_,)
    await client.write(writes=(tuple_,))
    assert seen[0][1]["tuple_key"] == tuple_.as_json()
    assert seen[1][1]["writes"]["tuple_keys"] == [tuple_.as_json()]
    assert seen[1][1]["authorization_model_id"] == "model-1"


@pytest.mark.asyncio
async def test_list_objects_rejects_wrong_object_type():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"objects": ["chat:not-a-workflow"]})

    with pytest.raises(
        OpenFgaUnavailableError,
        match="authorization_invalid_response",
    ):
        await _client(handler).list_objects(
            user="user:alice",
            relation="can_view",
            object_type="workflow",
            consistency=ConsistencyPreference.MINIMIZE_LATENCY,
        )


@pytest.mark.asyncio
async def test_batch_failure_does_not_retry_or_use_partial_grants(monkeypatch):
    from unittest.mock import Mock
    from vibecanvas_api.authorization import openfga_client as module
    warning = Mock()
    monkeypatch.setattr(module.logger, 'warning', warning)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'result': {
            '0': {'allowed': True},
            '1': {'error': {'internal_error': 'internal_error', 'message': 'private database timeout'}}}})
    with pytest.raises(OpenFgaUnavailableError):
        await _client(handler).batch_check(
            [('user:alice', 'can_view', 'workflow:a'), ('user:alice', 'can_view', 'workflow:b')],
            consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
    assert len(requests) == 1
    assert 'private database timeout' not in str(warning.call_args)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['check', 'list_objects'])
@pytest.mark.parametrize('failure', ['timeout', 'server', 'configuration'])
async def test_authorization_failure_is_not_retried(operation, failure):
    requests = []
    def handler(request):
        requests.append(request)
        if failure == 'timeout':
            raise httpx.ReadTimeout('private details', request=request)
        return httpx.Response(500 if failure == 'server' else 403, json={'code': 'error'})
    client = _client(handler)
    with pytest.raises(OpenFgaUnavailableError):
        if operation == 'check':
            await client.check(user='user:alice', relation='can_view', object_='workflow:a',
                               consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
        else:
            await client.list_objects(user='user:alice', relation='can_view', object_type='workflow',
                                      consistency=ConsistencyPreference.HIGHER_CONSISTENCY)
    assert len(requests) == 1
