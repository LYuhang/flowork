"""Loopback-only gateway fixture: real routes/Redis, fixed test identity and lease."""
import asyncio
from contextlib import asynccontextmanager
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import uvicorn

from vibecanvas_api.browser.cluster_registry import close_connections
from vibecanvas_api.config import config
from vibecanvas_api.routes import browser


async def main():
    config.redis.url = f"unix://{sys.argv[1]}"
    config.browser_token_secret = "isolated-relay-test"
    config.browser_extension_id = "test-extension"
    browser._browser_session_is_live = AsyncMock(return_value=True)
    browser._platform_session_is_live = AsyncMock(return_value=True)
    browser.verify_agent_capability = lambda *a, **kw: SimpleNamespace(
        organization_id="tenant", tenant_id="tenant", user_id="user", session_id="session",
        chat_id="test", session_generation=1, expires_at=time.time() + 60)
    browser.confirm_sidepanel_browser_session = AsyncMock()
    repo = AsyncMock()
    repo.get_browser_binding.return_value = dict(status="attached", browser_session_id="lease",
                                                browser_session_generation=1)
    browser.ChatRepo = lambda *a: repo

    @asynccontextmanager
    async def scope(**kw):
        yield None

    browser.session_scope = scope

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await close_connections()
            print(json.dumps({"closed": True}), flush=True)

    app = FastAPI(lifespan=lifespan)
    app.include_router(browser.transport_router)

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            print(json.dumps({"port": self.servers[0].sockets[0].getsockname()[1]}), flush=True)

    await Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")).serve()


if __name__ == "__main__":
    asyncio.run(main())
