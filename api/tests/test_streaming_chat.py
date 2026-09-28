"""Gate 3 — SSE streaming end-to-end via POST /chats/{chat_id}/messages.

What we can guarantee in a hermetic test environment (no LLM provider
installed): the run_turn fence is intact — `started` first, then either
`done` or `error` last. Mid-stream CHAT_UPDATE / VIBE_ACTION events
depend on an actual LLM round-trip, so we don't assert on them here.
The full LLM-driven event flow is covered by the demo app in legacy.

Auth: the legacy ``VIBECANVAS_API_DEV_TOKEN`` + sync ``TestClient`` +
``Bearer tok`` harness is DEAD. This now uses the conftest async ``client``
fixture + a real ``register → session_token``. No live LLM is needed: the
test only asserts on the ``started`` / ``done``|``error`` fence, which the
runtime emits regardless of whether an LLM round-trip succeeds.
"""

from __future__ import annotations

import json
import uuid

import pytest


async def _register(client) -> str:
    email = f"stream_{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": "Test User", "password": "pw12345678"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["session_token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _parse_sse_stream(byte_aiter) -> list[tuple[str, dict]]:
    """Parse raw SSE bytes into (event_name, data_dict) tuples."""
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


@pytest.mark.usefixtures("fake_codex_cli")
@pytest.mark.asyncio
async def test_chat_message_stream_has_started_and_terminator(client, pg_engine):
    tok = await _register(client)
    r = await client.post(
        "/api/v1/workflows", json={"name": "stream_wf"}, headers=_hdr(tok),
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
        json={"workflow": minimal_wf}, headers=_hdr(tok),
    )
    assert r.status_code == 200, r.text

    project = await client.post("/api/v1/projects", headers=_hdr(tok), json={"name": "Streaming"})
    assert project.status_code == 201, project.text
    async with client.stream(
        "POST", f"/api/v1/chat-scopes/{wf_id}/chats/c1/messages",
        json={"role": "user", "content": "hello", "project_id": project.json()["project_id"]}, headers=_hdr(tok),
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        events = await _parse_sse_stream(resp.aiter_bytes())

    names = [n for n, _ in events]
    assert names, "no SSE events received"
    assert names[0] == "started", f"first event not 'started': {names[:3]}"
    assert names[-1] in {"done", "error"}, (
        f"last event not done/error: {names[-2:]}"
    )


@pytest.mark.asyncio
async def test_resume_replays_full_event_history(client, pg_engine):
    pytest.skip(
        "resume-after-disconnect smoke deferred — TestClient mid-stream "
        "disconnect is finicky and the replay semantics are fully covered "
        "by test_async_turn_buffer.py + test_turn_runtime.py."
    )
