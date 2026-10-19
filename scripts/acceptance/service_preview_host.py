"""Manual HTTPS/browser acceptance fixture; requires a test certificate and key.

Set FLOWORK_SERVICE_PREVIEW_DOMAIN=preview.qa.test and PYTHONPATH to the tested
API/engine sources. This starts a separate bubblewrap sandbox, not a user project.
Authorization is replaced by a fixture session ONLY in this acceptance process;
production authorization is tested separately in test_service_forwarding.py.
Test artifacts and certificate paths are under /tmp/service-preview-*.
"""

import asyncio
import json
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path

import uvicorn
from vibecanvas_api.config import config
from vibecanvas_api.routes import sandbox_forwarding as gateway
from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
from vibecanvas_api.services.sandbox.bus_broker import BusBroker, RuntimeBusRouter, socket_path_for
from vibecanvas_api.services.sandbox.gvisor import _workflow_python_binds, _workflow_python_env


async def main():
    provider = BubblewrapProvider("/usr/bin/bwrap")
    identifier = "qa-preview-" + uuid.uuid4().hex
    broker = BusBroker(socket_path_for(identifier))
    await broker.start()
    env = _workflow_python_env()
    env["VC_BUS_SOCK"] = broker.socket_path
    argv = provider._build_argv(
        command=[sys.executable, "/qa/service_preview_guest.py"],
        env=env,
        network="none",
        rw_binds=[(str(Path(broker.socket_path).parent), str(Path(broker.socket_path).parent))],
        extra_ro_binds=list(_workflow_python_binds()),
        ro_dest_binds=[("/qa", str(Path(__file__).resolve().parent / "fixtures"))],
    )
    proc = await asyncio.create_subprocess_exec(*argv)
    try:
        async with asyncio.timeout(30):
            await broker.wait_connected()
        router = RuntimeBusRouter(broker)

        class Session:
            async def service_forwarding_stream(self, request):
                turn = router.attach(request["identifier"])
                try:
                    await turn.send({"type": "service_forwarding", "request": request})
                    async for item in turn.messages():
                        yield item
                        if item["type"] in {"error", "end"}:
                            break
                        if not request.get("websocket"):
                            await turn.send({"type": "service_forwarding_control", "action": "ack"})
                finally:
                    with suppress(Exception):
                        await turn.send({"type": "service_forwarding_control", "action": "cancel"})
                    turn.detach()

            async def service_forwarding_control(self, identifier, message):
                turn = router.turns.get(identifier)
                if turn:
                    await turn.send({**message, "type": "service_forwarding_control"})

        session = Session()

        async def authorized(claims):
            return session

        gateway.authorized_session = authorized
        config.public_urls.public_url = "https://app.qa.test:19443"
        claims = dict(
            tenant="qa",
            user="qa",
            project="qa",
            scope="qa",
            runtime_id=identifier,
            port=3100,
            scheme="http",
            path="/",
            exp=int(time.time()) + 3600,
        )
        token = gateway.codec().encrypt(json.dumps(claims).encode()).decode()
        host = gateway.hostname(token)
        Path("/tmp/service-preview-live.json").write_text(
            json.dumps(
                {
                    "url": f"https://{host}:19443/_flowork_preview/open?token={token}",
                    "host": host,
                    "port": 3100,
                    "pid": proc.pid,
                }
            )
        )
        from starlette.responses import HTMLResponse

        async def qa_app(scope, receive, send):
            if dict(scope.get("headers", [])).get(b"host", b"").startswith(b"app.qa.test"):
                response = HTMLResponse(
                    f'<h2>Service Preview</h2><iframe title="Live preview" style="width:90vw;height:80vh" sandbox="allow-scripts allow-same-origin allow-forms" src="https://{host}:19443/_flowork_preview/open?token={token}"></iframe>'
                )
                await response(scope, receive, send)
            else:
                await gateway.gateway(scope, receive, send)

        server = uvicorn.Server(
            uvicorn.Config(
                qa_app,
                host="127.0.0.1",
                port=19443,
                ssl_keyfile="/tmp/service-preview-key.pem",
                ssl_certfile="/tmp/service-preview-cert.pem",
                log_level="warning",
            )
        )
        print("READY isolated sandbox + HTTPS gateway", flush=True)
        await server.serve()
        await router.close()
    finally:
        await broker.close()
        if proc.returncode is None:
            proc.terminate()
        await proc.wait()


asyncio.run(main())
