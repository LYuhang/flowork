"""Bounded agent loop used only by the workflow ``SubAgentNode``.

The selectable Agent Runtime subsystem does not import this module. It is kept
as a lazy workflow-sandbox implementation until SubAgentNode itself is moved to
the unified Runtime protocol.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field, create_model

from vibecanvas_api.agents.tools.subagent.output import coerce_to_fields
from vibecanvas_api.services.platform_mcp.tool_runtime import AgentContext


@dataclass
class SubAgentResult:
    status: str
    output: dict
    trace: list[dict] = field(default_factory=list)
    error: str | None = None


_TYPE_MAP = {
    "string": str,
    "str": str,
    "integer": int,
    "int": int,
    "number": float,
    "float": float,
    "boolean": bool,
    "bool": bool,
}


def _message_text(message: Any) -> str:
    text = getattr(message, "text", None)
    if isinstance(text, str) and text:
        return text
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text") or block.get("content") or "")
            if isinstance(block, dict)
            else str(block)
            for block in content
        )
    return ""


def _messages_to_trace(messages: list) -> list[dict]:
    trace: list[dict] = []
    for message in messages:
        calls = []
        for call in getattr(message, "tool_calls", None) or []:
            calls.append({
                "name": call.get("name", "") if isinstance(call, dict) else getattr(call, "name", ""),
                "args": call.get("args", {}) if isinstance(call, dict) else getattr(call, "args", {}),
            })
        trace.append({
            "role": getattr(message, "type", None) or getattr(message, "role", ""),
            "text": _message_text(message),
            "tool_calls": calls,
        })
    return trace


def _system_message(prompt: str, output_fields: dict, tool_name: str) -> str:
    fields = []
    for name, raw_spec in output_fields.items():
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        type_name = str(spec.get("type") or "")
        description = str(spec.get("description") or "")
        fields.append(
            f"- {name}"
            + (f" ({type_name})" if type_name else "")
            + (f": {description}" if description else "")
        )
    contract = (
        f"## Output\nWhen complete, call `{tool_name}` exactly once with these fields:\n"
        + "\n".join(fields)
    )
    return f"{prompt.rstrip()}\n\n{contract}" if prompt.strip() else contract


def _make_output_tool(output_fields: dict, holder: dict[str, dict]):
    from langchain_core.tools import StructuredTool

    schema_fields: dict[str, Any] = {}
    for name, raw_spec in output_fields.items():
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        value_type = _TYPE_MAP.get(str(spec.get("type") or "string").lower(), str)
        default: Any = "" if value_type is str else (False if value_type is bool else 0)
        schema_fields[name] = (
            value_type | None,
            Field(default=default, description=str(spec.get("description") or "")),
        )
    schema = create_model("WorkflowSubAgentOutput", **schema_fields)

    async def set_output(**kwargs) -> str:
        holder["output"] = coerce_to_fields(kwargs, output_fields)
        return "Output recorded."

    return StructuredTool.from_function(
        coroutine=set_output,
        name="set_output",
        description="Record the final structured result and finish the task.",
        args_schema=schema,
        return_direct=True,
    )


async def run_bounded_agent(
    *,
    model,
    tools,
    system_prompt: str,
    user_input: str,
    output_fields: dict,
    max_iterations: int = 25,
    context: AgentContext | None = None,
    checkpointer: Any = None,
    thread_id: str | None = None,
    on_trace: Callable[[dict], Awaitable[None]] | None = None,
    request_tool_approval: Callable[[str, str, dict], Awaitable[str]] | None = None,
) -> SubAgentResult:
    """Run a stateless, bounded workflow worker to a structured result."""
    from langchain.agents import create_agent
    from langgraph.errors import GraphRecursionError

    if checkpointer is not None or thread_id is not None:
        raise ValueError("workflow subagents do not own persistent Runtime state")
    if request_tool_approval is not None:
        raise ValueError("workflow subagents cannot request interactive approval")

    ctx = context or AgentContext()
    stop_event = ctx.stop_event
    if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
        return SubAgentResult(
            status="error",
            output=coerce_to_fields({}, output_fields),
            error="cancelled",
        )

    holder: dict[str, dict] = {}
    output_tool = _make_output_tool(output_fields, holder)
    agent = create_agent(
        model=model,
        tools=[*tools, output_tool],
        context_schema=AgentContext,
    )
    messages = [
        {"role": "system", "content": _system_message(system_prompt, output_fields, output_tool.name)},
        {"role": "user", "content": user_input},
    ]
    config = {"recursion_limit": max(3, int(max_iterations) * 2 + 1)}

    try:
        if on_trace is None:
            result = await agent.ainvoke(
                {"messages": messages},
                context=ctx,
                config=config,
            )
        else:
            result = {"messages": []}
            published = 0
            async for state in agent.astream(
                {"messages": messages},
                context=ctx,
                config=config,
                stream_mode="values",
            ):
                if not isinstance(state, dict):
                    continue
                result = state
                trace = _messages_to_trace(list(state.get("messages") or []))
                for entry in trace[published:]:
                    await on_trace(entry)
                published = len(trace)
    except GraphRecursionError:
        return SubAgentResult(
            status="incomplete",
            output=coerce_to_fields(holder.get("output") or {}, output_fields),
            error="max_iterations reached",
        )
    except Exception as exc:
        if "output" in holder:
            return SubAgentResult("done", holder["output"])
        return SubAgentResult(
            status="error",
            output=coerce_to_fields({}, output_fields),
            error=f"{type(exc).__name__}: {exc}",
        )

    trace = _messages_to_trace(list(result.get("messages") or []))
    if "output" in holder:
        return SubAgentResult("done", holder["output"], trace=trace)
    return SubAgentResult(
        status="incomplete",
        output=coerce_to_fields({}, output_fields),
        trace=trace,
    )
