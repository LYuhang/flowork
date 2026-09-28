from __future__ import annotations

import asyncio

import pytest

from vibecanvas_api.services.agent_runtime import sandbox_entry as entry
from vibecanvas_api.services.agent_runtime.protocol import RuntimeTurnRequest
from vibecanvas_api.services.sandbox.bus_broker import RuntimeBusRouter


def request(chat: str) -> dict:
    return dict(tenant_id="tenant", user_id="user", chat_id=chat, turn_id=chat,
                runtime_type="codex", runtime_session_id="session",
                runtime_root="/runtime/.codex", message={"role": "user", "content": "test"},
                model={"id": "test", "connection_type": "chatgpt_account"})


class Channel:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def recv(self):
        return await self.incoming.get()

    async def send(self, message):
        await self.outgoing.put(message)


@pytest.mark.asyncio
async def test_guest_routes_controls_and_cancel_without_stopping_sibling(monkeypatch):
    channel = Channel()
    cancelled = []

    async def run(turn, req):
        await turn.send({"type": "runtime_event", "event": {"chat": req.chat_id}})
        try:
            response = await turn.recv()
            await turn.send({"type": "runtime_result", "value": response["response"]["value"]})
        except asyncio.CancelledError:
            cancelled.append(req.chat_id)
            raise

    monkeypatch.setattr(entry, "_run", run)
    task = asyncio.create_task(entry.serve_turns(channel))
    try:
        for chat in ("a", "b"):
            await channel.incoming.put({"type": "runtime_request", "request": request(chat)})
        events = [await asyncio.wait_for(channel.outgoing.get(), 1) for _ in range(2)]
        assert {event["turn_id"] for event in events} == {"a", "b"}
        await channel.incoming.put({"type": "runtime_control", "turn_id": "a",
                                    "response": {"action": "cancel"}})
        result = await asyncio.wait_for(channel.outgoing.get(), 1)
        assert result["turn_id"] == "a" and result["cancelled"]
        assert cancelled == ["a"]
        await channel.incoming.put({"type": "runtime_control", "turn_id": "b",
                                    "response": {"action": "accepted", "value": "B survived"}})
        result = await asyncio.wait_for(channel.outgoing.get(), 1)
        assert result["turn_id"] == "b" and result["value"] == "B survived"
    finally:
        await channel.incoming.put(None)
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_shared_bus_drops_detached_frames_and_broadcasts_transport_failure():
    class Broker(Channel):
        async def messages(self):
            while True:
                item = await self.recv()
                if item is None:
                    return
                yield item

    broker = Broker()
    router = RuntimeBusRouter(broker)
    a, b = router.attach("a"), router.attach("b")
    a.detach()
    stream = b.messages()
    await broker.incoming.put({"turn_id": "a", "type": "runtime_result"})
    await broker.incoming.put({"turn_id": "b", "type": "runtime_event"})
    assert (await asyncio.wait_for(anext(stream), 1))["turn_id"] == "b"
    await b.send({"type": "runtime_control", "response": {"action": "cancel"}})
    assert (await broker.outgoing.get())["turn_id"] == "b"
    await broker.incoming.put(None)
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(anext(stream), 1)
    await router.close()


@pytest.mark.asyncio
async def test_each_chat_owns_its_cli_and_mcp_resources(monkeypatch):
    from vibecanvas_api.services.agent_runtime import codex, mcp_hub_adapter

    clients = []
    running = asyncio.Queue()
    finish = asyncio.Event()

    class Resource:
        def __init__(self, *_args):
            self.closed = False

        async def close(self):
            self.closed = True

    def create(_req):
        client = Resource()
        clients.append(client)
        return client

    async def run(_channel, req, **kwargs):
        await running.put((req.chat_id, kwargs))
        await finish.wait()

    monkeypatch.setattr(codex, "create_codex_app_server", create)
    monkeypatch.setattr(codex, "run_codex_turn", run)
    monkeypatch.setattr(entry, "SandboxMcpHub", Resource)
    monkeypatch.setattr(mcp_hub_adapter, "SandboxMcpRuntimeAdapter", Resource)
    monkeypatch.setattr(entry, "_chat_runtimes", {})
    tasks = [asyncio.create_task(entry._run(None, RuntimeTurnRequest.model_validate(request(chat))))
             for chat in ("a", "b")]
    try:
        first = await asyncio.wait_for(running.get(), 1)
        second = await asyncio.wait_for(running.get(), 1)
        assert first[1]["client"] is not second[1]["client"]
        assert first[1]["mcp_adapter"] is not second[1]["mcp_adapter"]
        assert first[1]["hub_gateway_registry"] is not second[1]["hub_gateway_registry"]
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        assert clients[0].closed and not clients[1].closed
        finish.set()
        await asyncio.wait_for(tasks[1], 1)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for runtime in entry._chat_runtimes.values():
            await runtime.close()


def test_chat_capabilities_and_skill_homes_are_independent(monkeypatch, tmp_path):
    from vibecanvas_api.services.agent_runtime import codex

    monkeypatch.setattr(codex, "_BROKER_CAPABILITY_DIR", str(tmp_path))
    monkeypatch.setattr(codex, "_BROKER_CAPABILITY_PATH", str(tmp_path / "capability"))
    a, b = [RuntimeTurnRequest.model_validate(request(chat)) for chat in ("a", "b")]
    pa, pb = codex._broker_capability_path(a), codex._broker_capability_path(b)
    assert pa != pb
    codex._install_broker_capability("A", pa)
    codex._install_broker_capability("B", pb)
    codex._remove_broker_capability(pa)
    from pathlib import Path
    assert Path(pb).read_text() == "B"
    assert codex._codex_env(a.runtime_root, a.chat_id)["HOME"] != codex._codex_env(b.runtime_root, b.chat_id)["HOME"]
