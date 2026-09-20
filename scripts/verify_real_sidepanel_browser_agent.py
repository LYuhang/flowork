#!/usr/bin/env python3
"""Visible end-to-end acceptance for the real Flowork side-panel Agent.

This is deliberately separate from ``verify_playwright_mcp_real_browser.py``:
that verifier proves every reviewed official MCP tool without a model, while
this one proves the full user path through Web auth, the MV3 side panel, the
Agent Runtime, the authenticated CDP relay, a public website and VFS evidence.

Credentials are read interactively and are never persisted by this script.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from playwright.async_api import BrowserContext, Frame, Page, async_playwright


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension" / "dist"
EVIDENCE = ROOT / "output" / "playwright" / "sidepanel-browser-agent"
WEB_BASE = os.environ.get("FLOWORK_ACCEPTANCE_WEB_BASE", "http://localhost:9001")


def _scenario_fixture_html() -> bytes:
    return b"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Flowork User Journey Fixture</title></head><body>
<main><h1>COMMON_BROWSER_JOURNEY</h1>
<label>Name <input id="name" aria-label="Name"></label>
<label>Mode <select id="mode" aria-label="Mode"><option>Basic</option><option>Advanced</option></select></label>
<label>Command <input id="command" aria-label="Command"></label>
<button id="run">Run action</button><output id="action-result"></output>
<button id="dialog">Confirm action</button><output id="dialog-result"></output>
<button id="popup">Open result details</button></main>
<script>
const q=id=>document.getElementById(id);
q('run').onclick=()=>q('action-result').textContent=
  q('name').value==='Flowork User'&&q('mode').value==='Advanced'?'FORM_OK':'FORM_BAD';
q('command').onkeydown=e=>{if(e.key==='Enter')q('action-result').textContent+=' KEY_OK'};
q('dialog').onclick=()=>{if(confirm('Accept deterministic action?'))q('dialog-result').textContent='DIALOG_OK'};
q('popup').onclick=()=>window.open('/details','_blank');
</script></body></html>"""


async def _scenario_http(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        request = (await reader.readline()).decode("latin1", "replace").split(" ")
        path = request[1].split("?", 1)[0] if len(request) >= 2 else "/"
        while await reader.readline() not in {b"\r\n", b"\n", b""}:
            pass
        if path == "/details":
            body = b"<!doctype html><title>Result Details</title><h1>POPUP_OK</h1>"
        else:
            body = _scenario_fixture_html()
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


def _chromium_fallback(declared: str) -> str | None:
    expected = Path(declared)
    if expected.is_file():
        return None
    cached = sorted(
        Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome")
    )
    if not cached:
        raise RuntimeError("Chromium is missing; run `playwright install chromium`")
    return str(cached[-1])


async def _wait_for_embed(panel: Page) -> Frame:
    for _ in range(120):
        for frame in panel.frames:
            if "/embed/chat" in frame.url:
                return frame
        await asyncio.sleep(0.25)
    raise AssertionError("the extension did not load its embedded Chat frame")


async def _login_main_app(page: Page, email: str, password: str) -> None:
    await page.goto(f"{WEB_BASE}/login", wait_until="domcontentloaded")
    if "/login" not in page.url:
        return
    await page.locator("#login-email").fill(email)
    await page.locator("#login-password").fill(password)
    await page.get_by_role("button", name="Sign in", exact=True).click()
    await page.wait_for_url(lambda url: "/login" not in url, timeout=30_000)


async def _login_embed_if_needed(
    frame: Frame,
    email: str,
    password: str,
) -> None:
    email_input = frame.locator("#embed-login-email")
    try:
        await email_input.wait_for(state="visible", timeout=2_000)
    except Exception:
        return
    await email_input.fill(email)
    await frame.locator("#embed-login-password").fill(password)
    await frame.get_by_role("button", name="Sign in", exact=True).click()


async def _runtime_message(page: Page, payload: dict[str, object]) -> object:
    return await page.evaluate(
        "async value => await chrome.runtime.sendMessage(value)", payload
    )


async def _run(email: str, password: str) -> None:
    if not (EXTENSION / "service-worker.js").is_file():
        raise RuntimeError("extension/dist is missing; run `pnpm --dir extension build`")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="flowork-sidepanel-agent-"))
    context: BrowserContext | None = None
    scenario_server = await asyncio.start_server(_scenario_http, "127.0.0.1", 0)
    scenario_port = int(scenario_server.sockets[0].getsockname()[1])
    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(profile),
                executable_path=_chromium_fallback(
                    playwright.chromium.executable_path
                ),
                headless=False,
                viewport={"width": 1360, "height": 860},
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
            app = context.pages[0] if context.pages else await context.new_page()
            await _login_main_app(app, email, password)

            target = await context.new_page()
            await target.goto("https://example.com", wait_until="domcontentloaded")
            extension_id = worker.url.split("/")[2]
            panel = await context.new_page()
            await panel.goto(f"chrome-extension://{extension_id}/sidepanel.html")
            embed = await _wait_for_embed(panel)
            await _login_embed_if_needed(embed, email, password)

            composer = embed.locator('[data-role="agent-composer-input"]')
            try:
                await composer.wait_for(state="visible", timeout=45_000)
            except Exception:
                await panel.screenshot(
                    path=str(EVIDENCE / "sidepanel-bootstrap-failure.png"),
                    full_page=True,
                )
                diagnostics = {
                    "panel_url": panel.url,
                    "frames": [frame.url for frame in panel.frames],
                    "panel_text": (await panel.locator("body").inner_text()).strip(),
                    "embed_text": (await embed.locator("body").inner_text()).strip(),
                }
                (EVIDENCE / "bootstrap-failure.json").write_text(
                    json.dumps(diagnostics, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                raise
            await target.bring_to_front()

            completion_marker = f"SIDE_PANEL_ACCEPTANCE_{uuid.uuid4().hex[:12]}"
            prompt = (
                "/browser Use only the reviewed Playwright browser tools. "
                "Navigate to https://example.com, inspect the current page, "
                "report its exact heading and link text, click the More "
                "information link, verify that the final page belongs to "
                "iana.org, then take a full-page screenshot and save it as "
                "/data/browser-media/sidepanel-acceptance.png. Verify the "
                "result after every action, finish with a concise summary, "
                f"and include the exact marker {completion_marker}."
            )
            await composer.fill(prompt)
            await embed.locator(
                '[data-action="agent-composer-send"]'
            ).click()

            # The Agent must actually control the user's public tab. Transcript
            # text alone is not accepted as proof.
            await target.wait_for_url(
                lambda url: "iana.org" in str(url).lower(),
                timeout=240_000,
            )
            await target.screenshot(
                path=str(EVIDENCE / "controlled-public-page.png"),
                full_page=True,
            )

            # The marker appears once in the optimistic user bubble and a
            # second time only when the Agent obeys the final-answer request.
            # This prevents an earlier acceptance Turn in lazy-loaded history
            # from satisfying the wait and avoids opening tool groups while
            # the live transcript is still reconciling.
            transcript = ""
            for _ in range(480):
                transcript = (await embed.locator("body").inner_text()).strip()
                if (
                    transcript.count(completion_marker) >= 2
                    and "sidepanel-acceptance.png" in transcript
                ):
                    break
                await asyncio.sleep(0.5)
            else:
                raise AssertionError("the side panel never rendered the final Agent answer")

            for _ in range(60):
                if await composer.is_enabled():
                    break
                await asyncio.sleep(0.5)
            if not await composer.is_enabled():
                disabled_reason = embed.locator(
                    '[data-role="agent-composer-disabled-reason"]'
                )
                reason = (
                    (await disabled_reason.inner_text()).strip()
                    if await disabled_reason.count()
                    else ""
                )
                (EVIDENCE / "composer-disabled.json").write_text(
                    json.dumps(
                        {
                            "reason": reason,
                            "placeholder": await composer.get_attribute("placeholder"),
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                raise AssertionError(
                    f"the composer did not become reusable after the Turn: {reason}"
                )

            # Prove the official MCP standard-content result is projected as
            # an actual signed VFS image, not merely as a filename in the
            # transcript. Tool activity and the individual call are collapsed
            # after completion, so open the latest matching group/call first.
            groups = embed.locator('[data-action="tool-activity-toggle"]')
            for index in range(await groups.count() - 1, -1, -1):
                group = groups.nth(index)
                if (await group.get_attribute("aria-expanded")) != "true":
                    # Expanding one historical activity group remounts sibling
                    # rows because the disclosure state lives in the shared UI
                    # store. Dispatch through the freshly resolved node instead
                    # of Playwright's actionability loop, which correctly sees
                    # that remount as an unstable element and waits forever.
                    await group.evaluate("element => element.click()")
                    # The disclosure updates a shared React store. Give the
                    # corresponding tool-call subtree one render turn before
                    # opening the next group or querying its descendants.
                    await asyncio.sleep(0.1)

            screenshot_calls = embed.locator(
                '[data-role="tool-call"][data-tool-name="browser_take_screenshot"]'
            )
            try:
                await screenshot_calls.last.wait_for(state="attached", timeout=5_000)
            except Exception:
                pass
            # Call arguments are intentionally not repeated in the collapsed
            # row label, so select the newest screenshot in the completed
            # transcript. The unique completion marker above guarantees this
            # Turn has finished before we inspect it.
            screenshot_call = screenshot_calls.last
            if not await screenshot_call.count():
                await panel.screenshot(
                    path=str(EVIDENCE / "missing-screenshot-call.png"),
                    full_page=True,
                )
                tool_nodes = await embed.locator('[data-role="tool-call"]').evaluate_all(
                    "nodes => nodes.map(node => ({"
                    " tool: node.getAttribute('data-tool-name'),"
                    " text: (node.textContent || '').slice(0, 500)"
                    "}))"
                )
                (EVIDENCE / "missing-screenshot-call.json").write_text(
                    json.dumps(
                        {
                            "tool_activity_groups": await groups.count(),
                            "tool_calls": tool_nodes,
                            "transcript": transcript,
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                raise AssertionError(
                    "the completed Turn did not expose its screenshot tool call"
                )
            toggle = screenshot_call.locator('[data-action="tool-call-toggle"]')
            if (await toggle.get_attribute("aria-expanded")) != "true":
                await toggle.click()
            image = screenshot_call.locator('[data-role="browser-image"] img')
            await image.wait_for(state="visible", timeout=10_000)
            # The image deliberately uses native lazy loading. Scroll it into
            # the viewport before checking bytes; waiting on a newly attached
            # `load`/`error` listener can hang forever when the event fired
            # before the listener was installed.
            await image.scroll_into_view_if_needed()
            image_state: dict[str, object] = {}
            for _ in range(100):
                image_state = await image.evaluate(
                    "img => ({"
                    " complete: img.complete,"
                    " naturalWidth: img.naturalWidth,"
                    " naturalHeight: img.naturalHeight,"
                    " src: img.currentSrc || img.src"
                    "})"
                )
                if image_state.get("complete"):
                    break
                await asyncio.sleep(0.1)
            if not image_state.get("complete") or not image_state.get("naturalWidth"):
                await panel.screenshot(
                    path=str(EVIDENCE / "vfs-screenshot-render-failure.png"),
                    full_page=True,
                )
                (EVIDENCE / "vfs-screenshot-render-failure.json").write_text(
                    json.dumps(image_state, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                raise AssertionError(
                    f"the Playwright screenshot image did not load: {image_state}"
                )
            await screenshot_call.screenshot(
                path=str(EVIDENCE / "rendered-vfs-screenshot-card.png")
            )
            await panel.screenshot(
                path=str(EVIDENCE / "sidepanel-transcript.png"),
                full_page=True,
            )
            (EVIDENCE / "transcript.txt").write_text(
                transcript + "\n", encoding="utf-8"
            )
            if "iana" not in transcript.lower():
                raise AssertionError("Agent transcript did not verify the final domain")

            extended_journey = False
            if os.environ.get("FLOWORK_ACCEPTANCE_EXTENDED") == "1":
                # A second Turn on the same Chat proves the controller can be
                # recreated after the first Turn while the browser lease and
                # side-panel conversation remain usable. The deterministic
                # page models ordinary form, keyboard, dialog, and popup work.
                fixture_url = f"http://127.0.0.1:{scenario_port}/"
                await target.goto(fixture_url, wait_until="domcontentloaded")
                marker = f"COMMON_BROWSER_ACCEPTANCE_{uuid.uuid4().hex[:12]}"
                journey_prompt = (
                    "/browser Work only on the current deterministic acceptance page. "
                    "Observe it first. Fill Name with 'Flowork User', choose "
                    "Advanced mode, and click Run action; verify FORM_OK. Type "
                    "'RUN' in Command and press Enter; verify KEY_OK. Click "
                    "Confirm action, accept the JavaScript dialog, and verify "
                    "DIALOG_OK. Open result details, verify POPUP_OK in the new "
                    "tab, then select the original COMMON_BROWSER_JOURNEY tab "
                    "again. Re-observe after every state change and do not use "
                    f"evaluate or injected code. Finish with marker {marker}."
                )
                await composer.fill(journey_prompt)
                await embed.locator('[data-action="agent-composer-send"]').click()

                await target.locator("#action-result").wait_for(
                    state="visible", timeout=240_000
                )
                for _ in range(480):
                    result_text = (await target.locator("#action-result").inner_text()).strip()
                    dialog_text = (await target.locator("#dialog-result").inner_text()).strip()
                    transcript = (await embed.locator("body").inner_text()).strip()
                    popup_ok = False
                    for candidate in context.pages:
                        if candidate is target or candidate.is_closed():
                            continue
                        try:
                            popup_ok = popup_ok or await candidate.locator(
                                "text=POPUP_OK"
                            ).is_visible()
                        except Exception:
                            continue
                    if (
                        "FORM_OK" in result_text
                        and "KEY_OK" in result_text
                        and dialog_text == "DIALOG_OK"
                        and popup_ok
                        and transcript.count(marker) >= 2
                    ):
                        extended_journey = True
                        break
                    await asyncio.sleep(0.5)
                if not extended_journey:
                    await panel.screenshot(
                        path=str(EVIDENCE / "extended-journey-failure.png"),
                        full_page=True,
                    )
                    raise AssertionError(
                        "the model-driven form/dialog/popup journey did not complete"
                    )
                await target.screenshot(
                    path=str(EVIDENCE / "extended-common-journey.png"),
                    full_page=True,
                )

            storage_state = await _runtime_message(
                panel,
                {"type": "GET_BINDING"},
            )
            summary = {
                "target_url": target.url,
                "extension_id": extension_id,
                "sidepanel_loaded": True,
                "agent_controlled_public_page": True,
                "vfs_screenshot_rendered": True,
                "extended_common_journey": extended_journey,
                "binding_available": isinstance(storage_state, dict),
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
        scenario_server.close()
        await scenario_server.wait_closed()


def main() -> int:
    if os.environ.get("VIBECANVAS_EXTENSION_E2E") != "1":
        print("SKIP: set VIBECANVAS_EXTENSION_E2E=1 to launch a headed browser")
        return 0
    email = input("Acceptance account email: ").strip()
    password = getpass.getpass("Acceptance account password: ")
    if not email or not password:
        raise SystemExit("Both email and password are required")
    asyncio.run(_run(email, password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
