from __future__ import annotations

import threading

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from vibecanvas_api.agents.tool_runtime import AgentContext


def test_workflow_subagent_keeps_fixed_noninteractive_tool_surface():
    from vibecanvas_api.agents.tools.subagent.toolset import (
        SUBAGENT_DEFAULT_TOOL_NAMES,
        build_agent_subagent_tools,
    )

    assert SUBAGENT_DEFAULT_TOOL_NAMES == ("bash", "web_search", "read_images")
    tools = build_agent_subagent_tools()
    assert tuple(tool.name for tool in tools) == SUBAGENT_DEFAULT_TOOL_NAMES
    assert all("runtime" not in tool.args for tool in tools)
    assert set(tools[0].args) == {"command", "timeout_s"}


class _ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self


class _RaisingModel(_ScriptedModel):
    async def _agenerate(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("model exploded")


def _output_call(answer: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "set_output",
            "args": {"answer": answer},
            "id": "call_1",
            "type": "tool_call",
        }],
    )


@pytest.mark.asyncio
async def test_bounded_agent_stops_after_structured_output():
    from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent

    model = _ScriptedModel(messages=iter([
        _output_call("42"),
        AIMessage(content="must not be consumed"),
    ]))
    result = await run_bounded_agent(
        model=model,
        tools=[],
        system_prompt="be a worker",
        user_input="what is the answer?",
        output_fields={"answer": {"type": "string"}},
        max_iterations=10,
    )

    assert result.status == "done"
    assert result.output == {"answer": "42"}
    assert all("must not be consumed" not in item["text"] for item in result.trace)


@pytest.mark.asyncio
async def test_output_validation_can_be_repaired_instead_of_finishing_early():
    from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent

    result = await run_bounded_agent(
        model=_ScriptedModel(messages=iter([
            AIMessage(content="", tool_calls=[{
                "name": "set_output", "args": {}, "id": "missing", "type": "tool_call",
            }]),
            _output_call("repaired"),
            AIMessage(content="must not be consumed"),
        ])),
        tools=[], system_prompt="test", user_input="test",
        output_fields={"answer": {"type": "string"}}, max_iterations=4,
    )
    assert result.status == "done", result.error
    assert result.output == {"answer": "repaired"}
    assert any("'answer' is a required property" in e["text"] for e in result.trace)
    assert all("must not be consumed" not in e["text"] for e in result.trace)


@pytest.mark.asyncio
async def test_output_preserves_declared_array_and_object_types():
    from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent

    payload = {"items": [1, 2], "details": {"ok": True}, "count": 2}
    result = await run_bounded_agent(
        model=_ScriptedModel(messages=iter([AIMessage(content="", tool_calls=[{
            "name": "set_output", "args": payload, "id": "typed", "type": "tool_call",
        }])])),
        tools=[], system_prompt="test", user_input="test",
        output_fields={"items": {"type": "array"}, "details": {"type": "object"}, "count": {"type": "integer"}},
    )
    assert result.status == "done", result.error
    assert result.output == payload


@pytest.mark.asyncio
async def test_nested_output_schema_is_exposed_and_wrong_types_are_repairable():
    from vibecanvas_api.agents.tools.subagent.core import _make_output_tool, run_bounded_agent

    schema = {
        "type": "object", "required": ["total", "tags"],
        "properties": {"total": {"type": "number"}, "tags": {"type": "array", "items": {"type": "string"}}},
    }
    fields = {"updated_json": {"type": "object", "schema": schema}}
    tool = _make_output_tool(fields, {})
    assert tool.tool_call_schema["properties"]["updated_json"]["properties"] == schema["properties"]
    wrong = {"total": "49.35", "tags": {"item": ["receipt"]}}
    right = {"total": 49.35, "tags": ["receipt"]}
    result = await run_bounded_agent(
        model=_ScriptedModel(messages=iter([
            AIMessage(content="", tool_calls=[{
                "name": "set_output", "args": {"updated_json": value}, "id": f"output_{i}", "type": "tool_call",
            }]) for i, value in enumerate([wrong, right])
        ])),
        tools=[], system_prompt="test", user_input="test", output_fields=fields,
    )
    assert result.status == "done", result.error
    assert result.output == {"updated_json": right}
    assert any("Invalid output at updated_json" in entry["text"] for entry in result.trace)


@pytest.mark.asyncio
async def test_bounded_agent_does_not_leak_output_between_calls():
    from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent

    context = AgentContext()
    first = await run_bounded_agent(
        model=_ScriptedModel(messages=iter([_output_call("first")])),
        tools=[],
        system_prompt="be a worker",
        user_input="first",
        output_fields={"answer": {"type": "string"}},
        context=context,
    )
    second = await run_bounded_agent(
        model=_ScriptedModel(messages=iter([AIMessage(content="no output tool")])),
        tools=[],
        system_prompt="be a worker",
        user_input="second",
        output_fields={"answer": {"type": "string"}},
        context=context,
    )

    assert first.output == {"answer": "first"}
    assert second.status == "incomplete"
    assert second.output == {"answer": ""}


@pytest.mark.asyncio
async def test_bounded_agent_honors_preexisting_cancellation():
    from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent

    stop_event = threading.Event()
    stop_event.set()
    result = await run_bounded_agent(
        model=_RaisingModel(messages=iter([AIMessage(content="must not run")])),
        tools=[],
        system_prompt="be a worker",
        user_input="task",
        output_fields={"answer": {"type": "string"}},
        context=AgentContext(stop_event=stop_event),
    )

    assert result.status == "error"
    assert result.error == "cancelled"
    assert result.output == {"answer": ""}


@pytest.mark.asyncio
async def test_bounded_agent_executes_real_web_search_adapter(monkeypatch):
    import importlib

    from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent
    from vibecanvas_api.agents.tools.subagent.toolset import build_agent_subagent_tools

    search = importlib.import_module("vibecanvas_api.agents.tools.web.web_search")
    calls = []

    def fake_search(query, max_results):
        calls.append((query, max_results))
        return [{"title": "Source", "url": "https://example.com", "snippet": "Evidence"}]

    monkeypatch.setattr(search, "_search", fake_search)
    tools = build_agent_subagent_tools()
    for item in tools:
        assert "runtime" not in item.tool_call_schema.model_json_schema().get("properties", {})
    result = await run_bounded_agent(
        model=_ScriptedModel(messages=iter([
            AIMessage(content="", tool_calls=[{
                "name": "web_search", "args": {"query": "test", "max_results": 2},
                "id": "search_call", "type": "tool_call",
            }]),
            _output_call("found"),
        ])),
        tools=tools, system_prompt="test", user_input="test",
        output_fields={"answer": {"type": "string"}},
    )
    assert result.status == "done", result.error
    assert calls == [("test", 2)]
    assert any("Evidence" in entry["text"] for entry in result.trace)


@pytest.mark.asyncio
async def test_exhausted_loop_reports_tool_error_codes_without_contents():
    from langchain_core.tools import StructuredTool
    from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent

    async def failed_tool() -> tuple:
        return "private tool body", {
            "status": "error", "error": {"code": "no_workspace", "message": "private error"},
        }

    tool = StructuredTool.from_function(
        coroutine=failed_tool, name="probe", description="test",
        response_format="content_and_artifact",
    )
    result = await run_bounded_agent(
        model=_ScriptedModel(messages=iter([
            AIMessage(content="", tool_calls=[{
                "name": "probe", "args": {}, "id": f"call_{i}", "type": "tool_call",
            }]) for i in range(5)
        ])),
        tools=[tool], system_prompt="private prompt", user_input="private input",
        output_fields={"answer": {"type": "string"}}, max_iterations=1,
    )
    assert result.status == "incomplete"
    assert result.error.startswith("max_iterations reached")
    assert "probe:no_workspace=1" in result.error
    assert "private" not in result.error
    assert result.trace


def test_loop_diagnostics_rejects_freeform_error_codes():
    from langchain_core.messages import ToolMessage
    from vibecanvas_api.agents.tools.subagent.core import _tool_diagnostics

    message = ToolMessage(
        content="private body", tool_call_id="call", name="probe",
        artifact={"status": "error", "error": {"code": "private data: https://example.com"}},
    )
    assert _tool_diagnostics([message]) == (
        "; completed tools: probe=1; tool errors: probe:tool_error=1"
    )


@pytest.mark.asyncio
async def test_bounded_agent_stream_callback_does_not_duplicate_entries():
    from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent

    entries = []

    async def on_trace(entry):
        entries.append(entry)

    result = await run_bounded_agent(
        model=_ScriptedModel(messages=iter([_output_call("ok")])), tools=[],
        system_prompt="test", user_input="test",
        output_fields={"answer": {"type": "string"}}, on_trace=on_trace,
    )
    assert result.status == "done"
    assert entries == result.trace


@pytest.mark.asyncio
async def test_real_bash_tool_uses_workflow_workspace(monkeypatch, tmp_path):
    from vibecanvas_api.agents.tools import workspace_fs
    from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent
    from vibecanvas_api.agents.tools.subagent.toolset import build_agent_subagent_tools

    monkeypatch.delenv("VIBECANVAS_AGENT_RUNTIME_IN_SANDBOX", raising=False)
    monkeypatch.setattr(workspace_fs, "_WORKSPACE_ROOTS", (str(tmp_path),))
    result = await run_bounded_agent(
        model=_ScriptedModel(messages=iter([
            AIMessage(content="", tool_calls=[{
                "name": "bash", "args": {"command": "pwd"},
                "id": "bash_call", "type": "tool_call",
            }]),
            _output_call("ok"),
        ])),
        tools=build_agent_subagent_tools(working_dir=str(tmp_path)),
        system_prompt="test", user_input="test",
        output_fields={"answer": {"type": "string"}},
    )
    assert result.status == "done", result.error
    assert any(str(tmp_path) in entry["text"] for entry in result.trace)
    assert not any("could not be run" in entry["text"] for entry in result.trace)


@pytest.mark.asyncio
async def test_plain_text_finish_fails_without_an_extra_conversation_turn():
    from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent
    messages = iter([AIMessage(content="A plain answer is not completion"), _output_call("not consumed")])
    result = await run_bounded_agent(
        model=_ScriptedModel(messages=messages), tools=[], system_prompt="test", user_input="test",
        output_fields={"answer": {"type": "string"}}, max_iterations=5,
    )
    assert result.status == "incomplete"
    assert "set_output" in result.error
    assert result.output == {"answer": ""}
    assert next(messages).tool_calls[0]["args"]["answer"] == "not consumed"


@pytest.mark.asyncio
async def test_text_beside_output_call_is_trace_only_and_no_later_model_call_runs():
    from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent
    output = _output_call("42")
    output.content = "Accompanying explanation"
    messages = iter([output, AIMessage(content="unwanted follow-up")])
    result = await run_bounded_agent(
        model=_ScriptedModel(messages=messages), tools=[], system_prompt="test", user_input="test",
        output_fields={"answer": {"type": "string"}}, max_iterations=5,
    )
    assert result.status == "done"
    assert result.output == {"answer": "42"}
    assert any(entry["text"] == "Accompanying explanation" for entry in result.trace)
    assert all("unwanted follow-up" not in entry["text"] for entry in result.trace)
    assert next(messages).content == "unwanted follow-up"


@pytest.mark.asyncio
async def test_model_retry_does_not_repeat_completed_business_tool():
    from langchain_core.tools import tool
    from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent
    from vibecanvas_engine.model_retry import observations
    calls = []
    @tool
    def business_action() -> str:
        """Perform the test business side effect."""
        calls.append('executed')
        return 'ok'
    records = []
    token = observations.set(records)
    try:
        result = await run_bounded_agent(model=_ScriptedModel(messages=iter([
            AIMessage(content='', tool_calls=[{'name': 'business_action', 'args': {}, 'id': 'business', 'type': 'tool_call'}]),
            AIMessage(content=''), _output_call('done'),
        ])), tools=[business_action], system_prompt='test', user_input='test',
            output_fields={'answer': {'type': 'string'}}, retry=1)
    finally:
        observations.reset(token)
    assert result.status == 'done', result.error
    assert calls == ['executed']
    assert [record['attempts'] for record in records] == [1, 2]
    assert records[1]['failures'] == [{'attempt': 1, 'error_type': 'EmptyModelResponse'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['length', 'content_filter'])
async def test_subagent_does_not_retry_permanent_empty_completion(reason):
    from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent
    result = await run_bounded_agent(model=_ScriptedModel(messages=iter([
        AIMessage(content='', response_metadata={'finish_reason': reason}), _output_call('must not run'),
    ])), tools=[], system_prompt='test', user_input='test',
        output_fields={'answer': {'type': 'string'}}, retry=3)
    assert result.status == 'error'
    assert 'token limit or content filter' in result.error
