"""`/browser` command behaviour (post-redesign).

`/browser` is SIDE-PANEL-ONLY and additive (not a handoff):
  - MAIN APP (surface default "main"): refused with one `NOTICE` frame — no agent
    turn, no mode change — telling the user to use the side panel.
  - SIDE PANEL (surface="sidepanel"): activates browser mode + injects the
    browser tools, then runs a NORMAL agent turn (NO `MODE_CONTROL` handoff).
Pressing Send in the browser-only side panel explicitly authorizes its current
page for that Browser Turn. The API reserves a generation-fenced browser lease
after the Run is durable; the authenticated Playwright CDP handshake confirms
the lease only after the extension initializes. A normal (non-browser) message
is unaffected.

Auth: conftest async `client` + a real `register → session_token` (same harness
as test_streaming_chat.py).
"""

from __future__ import annotations

import json
import uuid

import pytest


async def _register(client) -> str:
    email = f"handoff_{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": "Test User", "password": "pw12345678"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["session_token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _parse_sse_stream(byte_aiter) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    buf = ""
    async for chunk in byte_aiter:
        buf += chunk if isinstance(chunk, str) else chunk.decode("utf-8")
        while "\n\n" in buf:
            block, _, buf = buf.partition("\n\n")
            ev_name: str | None = None
            data: dict | None = None
            for line in block.split("\n"):
                if line.startswith("event: "):
                    ev_name = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    raw = line.removeprefix("data: ")
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = {"raw": raw}
            if ev_name is not None:
                events.append((ev_name, data or {}))
    return events


async def _make_wf(client, tok) -> str:
    r = await client.post(
        "/api/v1/workflows", json={"name": "handoff_wf"}, headers=_hdr(tok),
    )
    wf_id = r.json()["wf_id"]
    minimal_wf = {
        "__meta__": {"workflow_id": wf_id, "workflow_version": 1,
                      "workflow_subversion": 0},
        "node_1": {
            "node_id": "node_1", "node_name": "__start__",
            "node_type": "StartNode", "node_description": "",
            "input_fields": {"m": {"type": "string", "value": "", "reference": ""}},
            "output_fields": {"m": {"type": "string", "description": ""}},
            "node_config": {"process_fn": ""}, "children": [],
            "__attributes__": {"x": 0, "y": 0},
        },
    }
    r = await client.post(
        f"/api/v1/workflows/{wf_id}/commits",
        json={"workflow": minimal_wf, "expected_version": "v1.sv0"}, headers=_hdr(tok),
    )
    assert r.status_code == 200, r.text
    return wf_id


@pytest.mark.asyncio
async def test_browser_command_in_main_app_emits_notice_and_skips_turn(client, pg_engine):
    """`/browser` is SIDE-PANEL-ONLY. From the MAIN APP (surface defaults to
    "main") it is REFUSED with a single NOTICE frame — no agent turn runs, no
    browser mode is activated — so the user is told to use the side panel."""
    tok = await _register(client)
    wf_id = await _make_wf(client, tok)

    async with client.stream(
        "POST", f"/api/v1/chat-scopes/{wf_id}/chats/c1/messages",
        json={"role": "user", "content": "/browser go book a flight",
              "agent_surface": "browser"},
        headers=_hdr(tok),
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        events = await _parse_sse_stream(resp.aiter_bytes())

    names = [n for n, _ in events]
    # Fenced by started/done; the ONLY substantive frame is the NOTICE.
    assert names[0] == "started", names
    assert names[-1] == "done", names
    assert "NOTICE" in names, names
    assert "MODE_CONTROL" not in names, names
    notice = next(p for n, p in events if n == "NOTICE")
    assert notice.get("code") == "browser_sidepanel_only", notice


@pytest.mark.usefixtures("fake_codex_cli")
@pytest.mark.asyncio
async def test_browser_command_in_side_panel_runs_turn(client, pg_engine):
    """From the SIDE PANEL (surface="sidepanel") `/browser` activates browser
    mode (additive) and runs a NORMAL agent turn — NO NOTICE, NO MODE_CONTROL."""
    tok = await _register(client)
    wf_id = await _make_wf(client, tok)

    async with client.stream(
        "POST", f"/api/v1/chat-scopes/{wf_id}/chats/c1/messages",
        json={"role": "user", "content": "/browser go book a flight",
              "surface": "sidepanel", "agent_surface": "browser"},
        headers=_hdr(tok),
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        events = await _parse_sse_stream(resp.aiter_bytes())

    names = [n for n, _ in events]
    assert names[0] == "started", names
    assert names[-1] in {"done", "error"}, names
    assert "NOTICE" not in names, names
    assert "MODE_CONTROL" not in names, names


@pytest.mark.usefixtures("fake_codex_cli")
@pytest.mark.asyncio
async def test_normal_message_unaffected_runs_agent_turn(client, pg_engine):
    tok = await _register(client)
    wf_id = await _make_wf(client, tok)
    project = await client.post("/api/v1/projects", headers=_hdr(tok), json={"name": "Streaming"})
    assert project.status_code == 201, project.text

    async with client.stream(
        "POST", f"/api/v1/chat-scopes/{wf_id}/chats/c2/messages",
        json={"role": "user", "content": "hello", "project_id": project.json()["project_id"]}, headers=_hdr(tok),
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        events = await _parse_sse_stream(resp.aiter_bytes())

    names = [n for n, _ in events]
    # The normal turn path is untouched: started first, done|error last, and NO
    # MODE_CONTROL frame.
    assert names[0] == "started", names
    assert names[-1] in {"done", "error"}, names
    assert "MODE_CONTROL" not in names, names
