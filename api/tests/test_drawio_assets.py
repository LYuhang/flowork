"""Local renderer assets are installation-owned, bounded and path-contained."""
import json
import struct

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from vibecanvas_api.routes.drawio_assets import router, _directory
from vibecanvas_api.security_headers import SecurityHeadersMiddleware


@pytest.fixture
def archive(tmp_path, monkeypatch):
    data = b'<html>local renderer</html>'
    node = {'files': {'index.html': {'offset': '0', 'size': len(data)}}}
    for name in reversed(('drawio', 'src', 'main', 'webapp')):
        node = {'files': {name: node}}
    header = json.dumps(node).encode()
    path = tmp_path / 'app.asar'
    path.write_bytes(struct.pack('<4I', 4, len(header) + 8, len(header) + 4, len(header)) + header + data)
    monkeypatch.setenv('DRAWIO_ASAR_PATH', str(path))
    _directory.cache_clear()
    return path


@pytest.mark.asyncio
async def test_serves_only_local_webapp_assets(archive):
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware, production=True)
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        result = await client.get('/api/v1/preview/drawio-assets/index.html')
        assert result.status_code == 200
        assert result.content == b'<html>local renderer</html>'
        assert result.headers['content-type'].startswith('text/html')
        assert "default-src 'self'" in result.headers['content-security-policy']
        assert "frame-ancestors 'self'" in result.headers['content-security-policy']
        assert result.headers['x-frame-options'] == 'SAMEORIGIN'
        for asset in ('missing.js', '%2e%2e/secret', 'js/%2e%2e/index.html', 'file%5cname', 'META-INF/private'):
            rejected = await client.get('/api/v1/preview/drawio-assets/' + asset)
            assert rejected.status_code == 404
            assert rejected.headers['x-frame-options'] == 'DENY'


@pytest.mark.asyncio
async def test_missing_or_invalid_installation_does_not_fallback(archive):
    archive.write_bytes(b'bad')
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        result = await client.get('/api/v1/preview/drawio-assets/index.html')
        assert result.status_code == 503
        assert result.json()['detail'] == 'drawio_renderer_unavailable'
