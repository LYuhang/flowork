"""Independent browser connection owner; no execution or migration lifecycle.

Run with ``uvicorn vibecanvas_api.browser.gateway:build_app --factory``.
The business API mints capabilities; this process verifies them and owns both
the extension and sandbox CDP WebSockets. Replicas share the existing directory.
"""

from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from vibecanvas_api.authorization.openfga_client import openfga_client_from_config
from vibecanvas_api.browser.cluster_registry import close_connections
from vibecanvas_api.config import config
from vibecanvas_api.observability import configure_logging
from vibecanvas_api.routes.browser import transport_router
from vibecanvas_api.security_profile import validate_browser_gateway_security
from vibecanvas_api.services.agent_resources.context import set_authorization_client
from vibecanvas_api.storage.db import dispose_engine, init_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        engine = init_engine()
        stack.push_async_callback(dispose_engine)
        if config.environment == "production":
            from vibecanvas_api.security.database_privileges import verify_database_role

            await verify_database_role(engine, mode="runtime")
        client = openfga_client_from_config()
        stack.push_async_callback(client.close)
        await client.probe()
        app.state.openfga_client = client
        set_authorization_client(client)
        stack.callback(set_authorization_client, None)
        # Stop transport work before disposing the authorization and DB clients.
        stack.push_async_callback(close_connections)
        yield


def build_app() -> FastAPI:
    configure_logging()
    validate_browser_gateway_security(config)
    app = FastAPI(title="Flowork Browser Gateway", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(transport_router)

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "service": "browser-gateway"}

    return app
