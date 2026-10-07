"""Request byte limits are independent of Content-Length and chunk boundaries."""
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from vibecanvas_api.routes.deployment_invoke import _read_body_with_hard_limit


def request(chunks, length=None):
    iterator = iter(chunks)
    async def receive():
        chunk = next(iterator, None)
        return {'type': 'http.request', 'body': chunk or b'', 'more_body': chunk is not None}
    headers = [] if length is None else [(b'content-length', str(length).encode())]
    return Request({'type': 'http', 'headers': headers}, receive=receive)


@pytest.mark.asyncio
@pytest.mark.parametrize('length', [None, 1, 100])
async def test_rejects_actual_oversize_without_consuming_rest(length):
    def chunks():
        yield b'123'
        yield b'456'
        pytest.fail('read continued beyond the size limit')
    with pytest.raises(HTTPException) as error:
        await _read_body_with_hard_limit(request(chunks(), length), limit=5)
    assert error.value.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize('chunks', [[], [b'12345'], [b'12', b'345']])
async def test_accepts_empty_and_exact_limit(chunks):
    assert await _read_body_with_hard_limit(request(chunks), limit=5) == b''.join(chunks)
