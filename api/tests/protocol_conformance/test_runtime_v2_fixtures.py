from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from vibecanvas_api.services.agent_runtime.codex_runtime import CodexSandboxRuntime
from vibecanvas_api.services.agent_runtime.orchestrator import _product_events
from vibecanvas_api.services.agent_runtime.protocol import (
    RuntimeControlResponse,
    RuntimeEvent,
    RuntimeOpenRequest,
    RuntimeTurnRequest,
    RuntimeType,
)


FIXTURE_ROOT = (
    Path(__file__).parents[1] / "fixtures" / "agent_runtime" / "v2"
)

RUNTIME_PROFILES = (
    pytest.param(
        RuntimeType.CODEX,
        CodexSandboxRuntime,
        "/runtime/.codex",
        {
            "id": "gpt-conformance",
            "base_url": "http://platform.test/api/internal/runtime-model/v1",
            "api_key": "turn-scoped-capability",
        },
        id="codex",
    ),
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _hydrate(value: Any, replacements: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _hydrate(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_hydrate(item, replacements) for item in value]
    if isinstance(value, str) and value in replacements:
        return replacements[value]
    return value


def _profile_replacements(
    runtime_type: RuntimeType,
    runtime_root: str,
    model: dict[str, Any],
) -> dict[str, Any]:
    return {
        "$runtime_type": runtime_type.value,
        "$runtime_root": runtime_root,
        "$model": model,
    }


def _read_events(path: Path, replacements: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _hydrate(json.loads(line), replacements)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _FixtureSandboxSession:
    def __init__(self, stream: list[dict[str, Any]]) -> None:
        self.stream = stream
        self.controls: list[tuple[str, dict[str, Any]]] = []
        self.cancelled: list[str] = []

    async def run_agent_runtime_stream(self, _request: dict[str, Any]):
        for event in self.stream:
            yield event

    async def send_agent_runtime_control(
        self,
        turn_id: str,
        response: dict[str, Any],
    ) -> None:
        self.controls.append((turn_id, response))

    async def cancel_agent_runtime(self, turn_id: str) -> bool:
        self.cancelled.append(turn_id)
        return True


@pytest.mark.parametrize(
    ("runtime_type", "_adapter_class", "runtime_root", "model"),
    RUNTIME_PROFILES,
)
def test_runtime_v2_open_and_turn_fixtures_are_adapter_neutral(
    runtime_type: RuntimeType,
    _adapter_class: type,
    runtime_root: str,
    model: dict[str, Any],
) -> None:
    replacements = _profile_replacements(runtime_type, runtime_root, model)

    opened = RuntimeOpenRequest.model_validate(
        _hydrate(_read_json(FIXTURE_ROOT / "open.json"), replacements)
    )
    turn = RuntimeTurnRequest.model_validate(
        _hydrate(_read_json(FIXTURE_ROOT / "turn-base.json"), replacements)
    )

    assert opened.runtime_type is runtime_type
    assert turn.runtime_type is runtime_type
    assert opened.runtime_session_id == turn.runtime_session_id
    assert opened.runtime_root == turn.runtime_root == runtime_root


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_type", "adapter_class", "runtime_root", "model"),
    RUNTIME_PROFILES,
)
async def test_every_runtime_adapter_preserves_canonical_v2_event_streams(
    runtime_type: RuntimeType,
    adapter_class: type,
    runtime_root: str,
    model: dict[str, Any],
) -> None:
    replacements = _profile_replacements(runtime_type, runtime_root, model)
    open_request = RuntimeOpenRequest.model_validate(
        _hydrate(_read_json(FIXTURE_ROOT / "open.json"), replacements)
    )
    turn_request = RuntimeTurnRequest.model_validate(
        _hydrate(_read_json(FIXTURE_ROOT / "turn-base.json"), replacements)
    )

    for scenario in _read_json(FIXTURE_ROOT / "scenarios.json"):
        raw_events = _read_events(FIXTURE_ROOT / scenario["file"], replacements)
        sandbox = _FixtureSandboxSession(raw_events)
        runtime = adapter_class(sandbox)
        await runtime.open(open_request)

        events = [event async for event in runtime.run_turn(turn_request)]

        assert [event.type for event in events] == scenario["event_types"]
        assert [event.seq for event in events] == list(range(1, len(events) + 1))
        assert all(event.runtime_type is runtime_type for event in events)
        assert all(event.chat_id == turn_request.chat_id for event in events)
        assert all(event.turn_id == turn_request.turn_id for event in events)
        assert all(
            event.runtime_session_id == turn_request.runtime_session_id
            for event in events
        )
        assert events[-1].type == scenario["terminal_type"]
        assert sum(
            event.type in {"runtime.completed", "runtime.failed"}
            for event in events
        ) == 1
        assert [event.model_dump(mode="json") for event in events] == raw_events


@pytest.mark.parametrize("scenario", _read_json(FIXTURE_ROOT / "scenarios.json"))
def test_canonical_v2_events_freeze_product_projection(scenario: dict[str, Any]) -> None:
    replacements = _profile_replacements(
        RuntimeType.CODEX,
        "/runtime/.codex",
        {},
    )
    events = [
        RuntimeEvent.model_validate(item)
        for item in _read_events(FIXTURE_ROOT / scenario["file"], replacements)
    ]
    product_event_types: list[str] = []

    for event in events:
        if event.type == "runtime.failed":
            with pytest.raises(RuntimeError, match=scenario["failure_message"]):
                _product_events(event)
            continue
        product_event_types.extend(name for name, _payload in _product_events(event))

    assert product_event_types == scenario["product_event_types"]


def test_failure_fixture_is_sanitized_and_bounded() -> None:
    raw = (FIXTURE_ROOT / "sanitized-failure.jsonl").read_text(encoding="utf-8")
    lowered = raw.casefold()
    assert len(raw.encode("utf-8")) <= 4_096
    assert all(
        marker not in lowered
        for marker in ("api_key", "authorization", "bearer ", "sk-", "private-")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_type", "adapter_class", "runtime_root", "model"),
    RUNTIME_PROFILES,
)
async def test_every_runtime_adapter_uses_the_same_control_and_cancel_contract(
    runtime_type: RuntimeType,
    adapter_class: type,
    runtime_root: str,
    model: dict[str, Any],
) -> None:
    replacements = _profile_replacements(runtime_type, runtime_root, model)
    sandbox = _FixtureSandboxSession([])
    runtime = adapter_class(sandbox)
    await runtime.open(
        RuntimeOpenRequest.model_validate(
            _hydrate(_read_json(FIXTURE_ROOT / "open.json"), replacements)
        )
    )
    response = RuntimeControlResponse(
        request_id="hitl-conformance",
        chat_id="chat-conformance",
        turn_id="turn-conformance",
        gate_type="pre_tool_approval",
        action="approve",
        correlation={
            "source": runtime_type.value,
            "runtime_request_id": "native-request",
            "runtime_method": "tool/approval",
        },
    )

    await runtime.respond(response)
    assert await runtime.cancel("turn-conformance") is True

    assert sandbox.controls == [
        ("turn-conformance", response.model_dump(mode="json"))
    ]
    assert sandbox.cancelled == ["turn-conformance"]
