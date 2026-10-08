"""Actual separate Uvicorn processes; auth/lease fixtures, not Chrome acceptance."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import signal

from websockets.asyncio.client import connect

from vibecanvas_api.browser.envelope import decode, encode
from vibecanvas_api.browser.scoped_token import mint_scoped_token
from vibecanvas_api.browser.ws_auth import build_browser_ws_protocols


@asynccontextmanager
async def api_process(socket):
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).parent / "fixtures/relay_api.py"), socket,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 20)
        assert line, (await process.stderr.read()).decode()
        yield json.loads(line)["port"]
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        assert process.returncode in (0, -signal.SIGTERM), (await process.stderr.read()).decode()
        assert json.loads(await process.stdout.readline()) == {"closed": True}


async def test_extension_and_cdp_roundtrip_across_two_api_processes(directory):
    shared, socket = directory
    token = mint_scoped_token("user", "tenant", "workflow", "isolated-relay-test",
                             browser_id="browser", extension_id="test-extension",
                             session_id="session", session_generation=1, session_audience="extension")
    async with api_process(socket) as first, api_process(socket) as second:
        async with connect(f"ws://127.0.0.1:{first}/api/v1/browser/ws",
                           subprotocols=build_browser_ws_protocols(token, "browser"),
                           origin="chrome-extension://test-extension") as extension:
            auth = decode(await asyncio.wait_for(extension.recv(), 3))
            assert auth["data"]["type"] == "auth_status"
            async with connect(f"ws://127.0.0.1:{second}/api/v1/browser/playwright/cdp",
                               additional_headers={"Authorization": "Bearer fixture"}) as cdp:
                initialize = decode(await asyncio.wait_for(extension.recv(), 3))
                assert initialize["data"]["action"] == "initialize"
                await extension.send(encode("playwright_relay", id="init", channel="chat:test",
                    transport="tenant:user:browser", data={"message": {"result": {"initialized": True}}}))
                ext_owner = await shared.get("tenant:user:browser")
                cdp_owner = await shared.get("tenant:user:browser", "chat:test")
                assert ext_owner.instance_id != cdp_owner.instance_id
                for index in range(3):
                    request = {"id": index, "method": "Browser.getVersion"}
                    await cdp.send(json.dumps(request))
                    frame = decode(await asyncio.wait_for(extension.recv(), 3))
                    assert frame["data"]["action"] == "request"
                    assert frame["data"]["request"] == request
                    response = {"id": index, "result": {"product": "isolated-browser"}}
                    await extension.send(encode("playwright_relay", id=str(index), channel="chat:test",
                        transport="tenant:user:browser", data={"message": response}))
                    assert json.loads(await asyncio.wait_for(cdp.recv(), 3)) == response
            closed = decode(await asyncio.wait_for(extension.recv(), 3))
            assert closed["data"]["action"] == "close"
            assert await shared.get("tenant:user:browser", "chat:test") is None
    assert await shared.get("tenant:user:browser") is None
