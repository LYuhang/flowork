#!/usr/bin/env python3
"""Real-browser acceptance for the pinned Playwright MCP + Flowork extension.

This verifier intentionally bypasses the application database and model. It
launches a headed Chromium with the unpacked production extension, connects the
exact pinned official MCP through Flowork's PLAYWRIGHT_RELAY_FRAME boundary,
and calls the same MCP tools exposed to LangChain and Codex.

Run after building the extension:

    pnpm --dir extension build
    VIBECANVAS_EXTENSION_E2E=1 \
      .venv/bin/python scripts/verify_playwright_mcp_real_browser.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from playwright.async_api import BrowserContext, Page, async_playwright


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension" / "dist"
LAUNCHER = ROOT / "api" / "playwright-runtime" / "launch.cjs"
EVIDENCE = ROOT / "output" / "playwright" / "browser-mcp-real"
CHANNEL = "chat:playwright-mcp-real"
SESSION_ID = "browser-session-playwright-mcp-real"
SESSION_GENERATION = 1
EXPECTED_TOOLS = {
    "browser_click",
    "browser_close",
    "browser_console_messages",
    "browser_drag",
    "browser_drop",
    "browser_evaluate",
    "browser_file_upload",
    "browser_fill_form",
    "browser_find",
    "browser_handle_dialog",
    "browser_hover",
    "browser_navigate",
    "browser_navigate_back",
    "browser_network_request",
    "browser_network_requests",
    "browser_press_key",
    "browser_resize",
    "browser_run_code_unsafe",
    "browser_select_option",
    "browser_snapshot",
    "browser_tabs",
    "browser_take_screenshot",
    "browser_type",
    "browser_wait_for",
}


def _fixture_html() -> bytes:
    return b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Flowork Browser MCP Fixture</title>
<style>body{font:16px system-ui;max-width:880px;margin:40px auto;padding:0 20px}button,input,select{margin:6px;padding:8px}iframe{width:100%;height:120px;border:1px solid #ccc}.box{display:inline-flex;align-items:center;justify-content:center;width:150px;height:64px;margin:8px;border:2px solid #6b7280;border-radius:8px}.drop{border-style:dashed}</style></head>
<body><h1>PLAYWRIGHT_MCP_FIXTURE</h1>
<label>Name <input id="name" aria-label="Name"></label>
<label>Mode <select id="mode" aria-label="Mode"><option>Basic</option><option>Advanced</option></select></label>
<label>Typed value <input id="typed" aria-label="Typed value"></label><output id="key-result"></output>
<button id="action">Run action</button><output id="action-result"></output>
<button id="hover">Hover target</button><output id="hover-result"></output>
<div id="drag-source" class="box" draggable="true">Drag source</div>
<div id="drag-target" class="box drop">Drag target</div><output id="drag-result"></output>
<div id="external-drop" class="box drop">External drop</div><output id="drop-result"></output>
<button id="wait">Start delayed result</button><output id="wait-result"></output>
<button id="console">Write console</button>
<button id="dialog">Open dialog</button><output id="dialog-result"></output>
<button id="popup">Open popup</button>
<button id="fetch">Fetch artifact</button><output id="fetch-result"></output>
<label>Upload <input id="upload" type="file" aria-label="Upload file"></label><output id="upload-result"></output>
<iframe title="Tool frame" src="/frame"></iframe>
<script>
const byId = id => document.getElementById(id);
byId('action').onclick=()=>byId('action-result').textContent='ACTION_OK';
byId('typed').onkeydown=event=>{if(event.key==='Enter')byId('key-result').textContent='KEY_OK'};
byId('hover').onmouseenter=()=>byId('hover-result').textContent='HOVER_OK';
byId('drag-source').ondragstart=event=>event.dataTransfer.setData('text/plain','DRAG_PAYLOAD');
byId('drag-target').ondragover=event=>event.preventDefault();
byId('drag-target').ondrop=event=>{event.preventDefault();byId('drag-result').textContent=event.dataTransfer.getData('text/plain')==='DRAG_PAYLOAD'?'DRAG_OK':'DRAG_BAD'};
byId('external-drop').ondragover=event=>event.preventDefault();
byId('external-drop').ondrop=event=>{event.preventDefault();byId('drop-result').textContent=event.dataTransfer.getData('text/plain')==='EXTERNAL_PAYLOAD'?'DROP_OK':'DROP_BAD'};
byId('wait').onclick=()=>setTimeout(()=>byId('wait-result').textContent='WAIT_OK',150);
byId('console').onclick=()=>console.info('CONSOLE_OK');
byId('dialog').onclick=()=>{alert('DIALOG_READY');byId('dialog-result').textContent='DIALOG_OK'};
byId('popup').onclick=()=>window.open('/popup','_blank');
byId('fetch').onclick=async()=>{byId('fetch-result').textContent=await (await fetch('/artifact')).text()};
byId('upload').onchange=async()=>{const f=byId('upload').files[0];byId('upload-result').textContent=f.name+':'+await f.text()};
</script></body></html>"""


async def _http_fixture(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        first = await reader.readline()
        request_line = first.decode("latin1", "replace").split(" ")
        if len(request_line) < 2:
            return
        path = request_line[1].split("?", 1)[0]
        while await reader.readline() not in {b"\r\n", b"\n", b""}:
            pass
        if path == "/frame":
            body = b'<!doctype html><button id="frame-action">Frame action</button><output id="frame-result"></output><script>document.querySelector("#frame-action").onclick=()=>document.querySelector("#frame-result").textContent="FRAME_OK"</script>'
            content_type = "text/html; charset=utf-8"
        elif path == "/popup":
            body = b"<!doctype html><title>Popup Result</title><h1>POPUP_OK</h1>"
            content_type = "text/html; charset=utf-8"
        elif path == "/artifact":
            body = b"NETWORK_ARTIFACT_OK\n"
            content_type = "text/plain; charset=utf-8"
        elif path == "/navigated":
            body = b"<!doctype html><title>Navigated</title><h1>NAVIGATED_OK</h1>"
            content_type = "text/html; charset=utf-8"
        else:
            body = _fixture_html()
            content_type = "text/html; charset=utf-8"
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def _runtime_message(page: Page, payload: dict[str, Any]) -> Any:
    return await page.evaluate(
        "async payload => await chrome.runtime.sendMessage(payload)", payload
    )


def _content_text(result: Any) -> str:
    chunks: list[str] = []
    for item in result.content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            chunks.append(text)
    return "\n".join(chunks)


def _expand_snapshot_links(text: str) -> str:
    expanded = [text]
    for link in re.findall(r"\[Snapshot\]\(([^)]+\.ya?ml)\)", text):
        candidate = Path(link)
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(ROOT.resolve())
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            expanded.append(resolved.read_text(encoding="utf-8"))
    return "\n".join(expanded)


async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> str:
    result = await session.call_tool(name, arguments)
    text = _expand_snapshot_links(_content_text(result))
    if result.isError:
        raise AssertionError(f"{name} failed:\n{text}")
    return text


def _ref(snapshot: str, label: str) -> str:
    for line in snapshot.splitlines():
        if label.lower() not in line.lower():
            continue
        match = re.search(r"\[ref=([^\]]+)\]", line)
        if match:
            return match.group(1)
    raise AssertionError(f"snapshot did not contain a ref for {label!r}:\n{snapshot}")


async def _run() -> None:
    if not EXTENSION.is_dir() or not (EXTENSION / "service-worker.js").is_file():
        raise RuntimeError("extension/dist is missing; run pnpm --dir extension build")
    if not LAUNCHER.is_file():
        raise RuntimeError("pinned Playwright MCP launcher is missing")

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    for item in EVIDENCE.iterdir():
        if item.is_file():
            item.unlink()

    http_server = await asyncio.start_server(_http_fixture, "127.0.0.1", 0)
    http_port = http_server.sockets[0].getsockname()[1]
    fixture_url = f"http://127.0.0.1:{http_port}/"
    relay_clients: set[Any] = set()
    panel: Page | None = None
    relay_sequence = 0

    async def relay_frame(action: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal relay_sequence
        assert panel is not None
        relay_sequence += 1
        raw = await _runtime_message(
            panel,
            {
                "type": "PLAYWRIGHT_RELAY_FRAME",
                "env": {
                    "v": 1,
                    "kind": "playwright_relay",
                    "id": f"accept-{relay_sequence}",
                    "channel": CHANNEL,
                    "transport": "real-browser-acceptance",
                    "data": {
                        "action": action,
                        "browser_session_id": SESSION_ID,
                        "session_generation": SESSION_GENERATION,
                        "request": request,
                    },
                },
            },
        )
        if not isinstance(raw, str):
            raise AssertionError(f"extension returned a non-string relay frame: {raw!r}")
        return json.loads(raw)["data"]["message"]

    async def relay_ws(websocket: Any) -> None:
        relay_clients.add(websocket)
        pending: set[asyncio.Task[None]] = set()

        async def handle_request(raw: str) -> None:
            response = await relay_frame("request", json.loads(raw))
            await websocket.send(json.dumps(response, separators=(",", ":")))

        try:
            try:
                async for raw in websocket:
                    task = asyncio.create_task(handle_request(raw))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
            except ConnectionClosed:
                pass
        finally:
            relay_clients.discard(websocket)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async with websockets.serve(relay_ws, "127.0.0.1", 0) as relay_server:
        relay_port = relay_server.sockets[0].getsockname()[1]
        relay_url = f"ws://127.0.0.1:{relay_port}"
        profile = Path(tempfile.mkdtemp(prefix="flowork-browser-mcp-real-"))
        context: BrowserContext | None = None
        try:
            async with async_playwright() as playwright:
                executable_path: str | None = None
                declared_executable = Path(playwright.chromium.executable_path)
                if not declared_executable.is_file():
                    cached = sorted(
                        Path.home().glob(
                            ".cache/ms-playwright/chromium-*/chrome-linux64/chrome"
                        )
                    )
                    if not cached:
                        raise RuntimeError(
                            "Chromium is missing; run `playwright install chromium`"
                        )
                    executable_path = str(cached[-1])
                context = await playwright.chromium.launch_persistent_context(
                    str(profile),
                    executable_path=executable_path,
                    headless=False,
                    args=[
                        f"--disable-extensions-except={EXTENSION}",
                        f"--load-extension={EXTENSION}",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                )
                worker = (
                    context.service_workers[0]
                    if context.service_workers
                    else await context.wait_for_event("serviceworker")
                )
                target = context.pages[0] if context.pages else await context.new_page()
                # This Playwright client only launches the extension test browser.
                # Keep dialogs open so the independently connected official MCP
                # remains their sole owner, matching the production topology.
                dialog_guard = lambda _dialog: None
                target.on("dialog", dialog_guard)
                await target.goto(fixture_url)
                extension_id = worker.url.split("/")[2]
                panel = await context.new_page()
                await panel.goto(f"chrome-extension://{extension_id}/sidepanel.html")

                async def receive_extension_frame(raw: str) -> None:
                    try:
                        envelope = json.loads(raw)
                        if envelope.get("kind") != "playwright_relay":
                            return
                        message = (envelope.get("data") or {}).get("message")
                        if not isinstance(message, dict):
                            return
                        payload = json.dumps(message, separators=(",", ":"))
                        await asyncio.gather(
                            *(client.send(payload) for client in tuple(relay_clients)),
                            return_exceptions=True,
                        )
                    except Exception:
                        return

                await panel.expose_function(
                    "__floworkReceiveRelayFrame", receive_extension_frame
                )
                await panel.evaluate(
                    """() => chrome.runtime.onMessage.addListener(message => {
                      if (message?.type === 'WS_SEND' && typeof message.raw === 'string')
                        globalThis.__floworkReceiveRelayFrame(message.raw);
                    })"""
                )
                tabs = await worker.evaluate(
                    "async url => await chrome.tabs.query({}).then(tabs => tabs.filter(tab => tab.url === url))",
                    fixture_url,
                )
                if not tabs or tabs[0].get("id") is None:
                    raise AssertionError("extension could not resolve the fixture tab")
                await _runtime_message(
                    panel,
                    {
                        "type": "SIDEPANEL_WINDOW",
                        "windowId": tabs[0]["windowId"],
                        "panelContextId": "playwright-mcp-real-panel",
                    },
                )
                await target.bring_to_front()
                initialized = await relay_frame("initialize")
                if initialized.get("result", {}).get("initialized") is not True:
                    raise AssertionError(f"relay initialization failed: {initialized}")

                env = dict(os.environ)
                env.update(
                    {
                        "FLOWORK_PLAYWRIGHT_CDP_ENDPOINT": relay_url,
                        "FLOWORK_PLAYWRIGHT_CDP_BEARER": "acceptance-only",
                    }
                )
                params = StdioServerParameters(
                    command="node",
                    args=[
                        str(LAUNCHER),
                        "--codegen",
                        "none",
                        "--snapshot-mode",
                        "full",
                        "--timeout-action",
                        "7000",
                        "--timeout-navigation",
                        "15000",
                        "--timeout-settle",
                        "100",
                        "--output-dir",
                        str(EVIDENCE),
                    ],
                    env=env,
                    cwd=str(ROOT),
                )
                async with stdio_client(params) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        actual_tools = {tool.name for tool in tools.tools}
                        if actual_tools != EXPECTED_TOOLS:
                            raise AssertionError(
                                f"pinned MCP tool drift: {sorted(actual_tools ^ EXPECTED_TOOLS)}"
                            )
                        exercised: set[str] = set()

                        snapshot = await _call(session, "browser_snapshot", {})
                        exercised.add("browser_snapshot")
                        if "PLAYWRIGHT_MCP_FIXTURE" not in snapshot:
                            raise AssertionError(snapshot)

                        await _call(
                            session,
                            "browser_fill_form",
                            {
                                "fields": [
                                    {
                                        "target": _ref(snapshot, "Name"),
                                        "name": "Name",
                                        "type": "textbox",
                                        "value": "Flowork User",
                                    },
                                    {
                                        "target": _ref(snapshot, "Mode"),
                                        "name": "Mode",
                                        "type": "combobox",
                                        "value": "Advanced",
                                    },
                                ]
                            },
                        )
                        exercised.add("browser_fill_form")
                        action_result = await _call(
                            session,
                            "browser_click",
                            {"target": _ref(snapshot, "Run action")},
                        )
                        exercised.add("browser_click")
                        if "ACTION_OK" not in action_result:
                            raise AssertionError(action_result)

                        found = await _call(
                            session, "browser_find", {"text": "Tool frame"}
                        )
                        exercised.add("browser_find")
                        if "Tool frame" not in found:
                            raise AssertionError(found)

                        selected = await _call(
                            session,
                            "browser_select_option",
                            {
                                "target": _ref(action_result, "Mode"),
                                "values": ["Basic"],
                            },
                        )
                        exercised.add("browser_select_option")
                        await _call(
                            session,
                            "browser_type",
                            {
                                "target": _ref(selected, "Typed value"),
                                "text": "TYPED_OK",
                                "slowly": True,
                            },
                        )
                        exercised.add("browser_type")
                        keyed = await _call(
                            session, "browser_press_key", {"key": "Enter"}
                        )
                        exercised.add("browser_press_key")
                        if "KEY_OK" not in keyed:
                            keyed = await _call(session, "browser_snapshot", {})
                        if "KEY_OK" not in keyed:
                            raise AssertionError(keyed)

                        hovered = await _call(
                            session,
                            "browser_hover",
                            {"target": _ref(keyed, "Hover target")},
                        )
                        exercised.add("browser_hover")
                        if "HOVER_OK" not in hovered:
                            raise AssertionError(hovered)

                        dragged = await _call(
                            session,
                            "browser_drag",
                            {
                                "startTarget": _ref(hovered, "Drag source"),
                                "endTarget": _ref(hovered, "Drag target"),
                            },
                        )
                        exercised.add("browser_drag")
                        if "DRAG_OK" not in dragged:
                            raise AssertionError(dragged)

                        dropped = await _call(
                            session,
                            "browser_drop",
                            {
                                "target": _ref(dragged, "External drop"),
                                "data": {"text/plain": "EXTERNAL_PAYLOAD"},
                            },
                        )
                        exercised.add("browser_drop")
                        if "DROP_OK" not in dropped:
                            raise AssertionError(dropped)

                        frame_result = await _call(
                            session,
                            "browser_click",
                            {"target": _ref(dropped, "Frame action")},
                        )
                        if "FRAME_OK" not in frame_result:
                            raise AssertionError(frame_result)

                        waiting = await _call(
                            session,
                            "browser_click",
                            {"target": _ref(frame_result, "Start delayed result")},
                        )
                        waited = await _call(
                            session, "browser_wait_for", {"text": "WAIT_OK"}
                        )
                        exercised.add("browser_wait_for")
                        if "WAIT_OK" not in waited:
                            raise AssertionError(f"{waiting}\n{waited}")

                        await _call(
                            session,
                            "browser_click",
                            {"target": _ref(waited, "Write console")},
                        )
                        console_messages = await _call(
                            session,
                            "browser_console_messages",
                            {"level": "info", "all": True},
                        )
                        exercised.add("browser_console_messages")
                        if "CONSOLE_OK" not in console_messages:
                            raise AssertionError(console_messages)

                        await _call(
                            session,
                            "browser_resize",
                            {"width": 1040, "height": 760},
                        )
                        exercised.add("browser_resize")
                        interaction_snapshot = await _call(
                            session, "browser_snapshot", {}
                        )

                        dialog_open = await _call(
                            session,
                            "browser_click",
                            {"target": _ref(interaction_snapshot, "Open dialog")},
                        )
                        if "DIALOG_READY" not in dialog_open:
                            raise AssertionError(dialog_open)
                        dialog_result = await _call(
                            session, "browser_handle_dialog", {"accept": True}
                        )
                        target.remove_listener("dialog", dialog_guard)
                        exercised.add("browser_handle_dialog")
                        dialog_snapshot = await _call(
                            session, "browser_snapshot", {}
                        )
                        if "DIALOG_OK" not in dialog_snapshot:
                            raise AssertionError(
                                f"dialog result={dialog_result}\n{dialog_snapshot}"
                            )

                        upload_source = EVIDENCE / "upload-source.txt"
                        upload_source.write_text("UPLOAD_PAYLOAD_OK", encoding="utf-8")
                        chooser = await _call(
                            session,
                            "browser_click",
                            {"target": _ref(dialog_snapshot, "Upload file")},
                        )
                        if "File chooser" not in chooser:
                            raise AssertionError(chooser)
                        upload_result = await _call(
                            session,
                            "browser_file_upload",
                            {"paths": [str(upload_source.resolve())]},
                        )
                        exercised.add("browser_file_upload")
                        if "upload-source.txt:UPLOAD_PAYLOAD_OK" not in upload_result:
                            raise AssertionError(upload_result)

                        popup_result = await _call(
                            session,
                            "browser_click",
                            {"target": _ref(upload_result, "Open popup")},
                        )
                        tabs_result = await _call(
                            session, "browser_tabs", {"action": "list"}
                        )
                        exercised.add("browser_tabs")
                        if "Popup Result" not in tabs_result and "POPUP_OK" not in popup_result:
                            raise AssertionError(tabs_result)

                        # Select the fixture tab again before testing its network log.
                        await _call(
                            session, "browser_tabs", {"action": "select", "index": 0}
                        )
                        current = await _call(session, "browser_snapshot", {})
                        fetch_result = await _call(
                            session,
                            "browser_click",
                            {"target": _ref(current, "Fetch artifact")},
                        )
                        if "NETWORK_ARTIFACT_OK" not in fetch_result:
                            raise AssertionError(fetch_result)
                        requests = await _call(
                            session,
                            "browser_network_requests",
                            {"static": False, "filter": "/artifact"},
                        )
                        exercised.add("browser_network_requests")
                        match = re.search(r"\[(\d+)\].*?/artifact", requests)
                        if not match:
                            # The exact display is version-owned; accept the first
                            # explicitly numbered artifact request as a fallback.
                            match = re.search(r"(?:^|\n)\s*(\d+)[\s.:].*?/artifact", requests)
                        if not match:
                            raise AssertionError(requests)
                        saved = await _call(
                            session,
                            "browser_network_request",
                            {
                                "index": int(match.group(1)),
                                "part": "response-body",
                                "filename": str(
                                    (EVIDENCE / "network-artifact.txt").resolve()
                                ),
                            },
                        )
                        exercised.add("browser_network_request")
                        artifact_path = EVIDENCE / "network-artifact.txt"
                        if not artifact_path.is_file() or "NETWORK_ARTIFACT_OK" not in artifact_path.read_text():
                            raise AssertionError(f"network response was not persisted: {saved}")

                        screenshot_result = await _call(
                            session,
                            "browser_take_screenshot",
                            {
                                "filename": str(
                                    (EVIDENCE / "playwright-mcp-real.png").resolve()
                                ),
                                "fullPage": True,
                                "scale": "css",
                            },
                        )
                        exercised.add("browser_take_screenshot")
                        screenshot_path = EVIDENCE / "playwright-mcp-real.png"
                        if not screenshot_path.is_file() or screenshot_path.stat().st_size < 1000:
                            raise AssertionError(
                                f"screenshot was not persisted: {screenshot_result}"
                            )

                        navigated = await _call(
                            session,
                            "browser_navigate",
                            {"url": f"http://127.0.0.1:{http_port}/navigated"},
                        )
                        exercised.add("browser_navigate")
                        if "NAVIGATED_OK" not in navigated:
                            raise AssertionError(navigated)
                        returned = await _call(
                            session, "browser_navigate_back", {}
                        )
                        exercised.add("browser_navigate_back")
                        if "PLAYWRIGHT_MCP_FIXTURE" not in returned:
                            raise AssertionError(returned)

                        new_tab = await _call(
                            session,
                            "browser_tabs",
                            {
                                "action": "new",
                                "url": f"http://127.0.0.1:{http_port}/navigated",
                            },
                        )
                        if "NAVIGATED_OK" not in new_tab:
                            new_tab_snapshot = await _call(
                                session, "browser_snapshot", {}
                            )
                            if "NAVIGATED_OK" not in new_tab_snapshot:
                                raise AssertionError(new_tab)
                        await _call(session, "browser_tabs", {"action": "close"})
                        await _call(
                            session, "browser_tabs", {"action": "select", "index": 0}
                        )

                        await _call(session, "browser_close", {})
                        exercised.add("browser_close")

                        forbidden = {
                            "browser_evaluate",
                            "browser_run_code_unsafe",
                        }
                        reviewed = actual_tools - forbidden
                        if len(reviewed) != 22:
                            raise AssertionError(f"unexpected reviewed surface: {reviewed}")
                        if exercised != reviewed:
                            raise AssertionError(
                                "real-browser coverage drift: "
                                f"missing={sorted(reviewed - exercised)}, "
                                f"unexpected={sorted(exercised - reviewed)}"
                            )

                await relay_frame("close")
                summary = {
                    "fixture": fixture_url,
                    "upstream_tool_count": len(EXPECTED_TOOLS),
                    "reviewed_tool_count": 22,
                    "checks": sorted(exercised),
                }
                (EVIDENCE / "acceptance.json").write_text(
                    json.dumps(summary, indent=2) + "\n", encoding="utf-8"
                )
                print(json.dumps(summary, ensure_ascii=False))
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            shutil.rmtree(profile, ignore_errors=True)
            http_server.close()
            await http_server.wait_closed()


def main() -> int:
    if os.environ.get("VIBECANVAS_EXTENSION_E2E") != "1":
        print("SKIP: set VIBECANVAS_EXTENSION_E2E=1 to launch a headed browser")
        return 0
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
