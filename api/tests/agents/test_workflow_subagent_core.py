from __future__ import annotations

import threading

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from vibecanvas_api.services.platform_mcp.tool_runtime import AgentContext


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
