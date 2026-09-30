"""Bounded agent loop used only by the workflow ``SubAgentNode``.

The selectable Agent Runtime subsystem does not import this module. It is kept
as a lazy workflow-sandbox implementation until SubAgentNode itself is moved to
the unified Runtime protocol.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from vibecanvas_api.agents.tools.subagent.output import coerce_to_fields
from vibecanvas_api.agents.tools.subagent.traces import chatml_messages
from vibecanvas_api.agents.tool_runtime import AgentContext


@dataclass
class SubAgentResult:
    status: str
    output: dict
    trace: list[dict] = field(default_factory=list)
    error: str | None = None
    messages: list[dict] = field(default_factory=list)


_TYPE_ALIASES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}


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
                "id": call.get("id", "") if isinstance(call, dict) else getattr(call, "id", ""),
                "name": call.get("name", "") if isinstance(call, dict) else getattr(call, "name", ""),
                "args": call.get("args", {}) if isinstance(call, dict) else getattr(call, "args", {}),
            })
        trace.append({
            "role": getattr(message, "type", None) or getattr(message, "role", ""),
            "tool_call_id": getattr(message, "tool_call_id", None),
            "status": getattr(message, "status", None),
            "text": _message_text(message),
            "tool_calls": calls,
        })
    return trace


def _tool_diagnostics(messages: list) -> str:
    """Summarize exhausted loops without echoing prompts, arguments or bodies."""
    counts: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    for message in messages:
        if getattr(message, "type", None) != "tool":
            continue
        name = str(getattr(message, "name", None) or "tool")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            name = "tool"
        counts[name] += 1
        artifact = getattr(message, "artifact", None)
        if isinstance(artifact, dict) and artifact.get("status") == "error":
            error = artifact.get("error") or {}
            code = str(error.get("code") or "tool_error") if isinstance(error, dict) else "tool_error"
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code):
                code = "tool_error"
            errors[f"{name}:{code}"] += 1
        elif getattr(message, "status", None) == "error":
            errors[f"{name}:tool_error"] += 1
    if not counts:
        return "; no completed tool calls"
    details = "; completed tools: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    if errors:
        details += "; tool errors: " + ", ".join(f"{k}={v}" for k, v in sorted(errors.items()))
    return details


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
    import jsonschema
    from langchain_core.tools import StructuredTool, ToolException

    properties = {}
    for name, raw_spec in output_fields.items():
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        field_type = str(spec.get("type") or "string").lower()
        properties[name] = {
            **(spec.get("schema") if isinstance(spec.get("schema"), dict) else {}),
            "type": _TYPE_ALIASES.get(field_type, field_type),
            "description": str(spec.get("description") or ""),
        }
    # Pass JSON Schema directly: the SDK's Pydantic subset-model conversion
    # discards field json_schema_extra, hiding nested properties from the model.
    schema = {
        "type": "object", "properties": properties,
        "required": list(properties), "additionalProperties": False,
    }
    validator = jsonschema.Draft202012Validator(schema)

    async def set_output(**kwargs) -> str:
        try:
            validator.validate(kwargs)
        except jsonschema.ValidationError as exc:
            path = ".".join(map(str, exc.absolute_path)) or "result"
            raise ToolException(f"Invalid output at {path}: {exc.message}") from exc
        holder["output"] = {name: kwargs[name] for name in output_fields}
        return "Output recorded."

    return StructuredTool.from_function(
        coroutine=set_output,
        name="set_output",
        description="Record the final structured result and finish the task.",
        args_schema=schema,
        handle_tool_error=True,
        # return_direct also stops on validation errors in LangChain. The loop
        # below instead stops only after this function records valid output.
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
    from vibecanvas_api.agents.tools.subagent.images import ToolImageMessages

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
        middleware=[ToolImageMessages()],
    )
    messages = [
        {"role": "system", "content": _system_message(system_prompt, output_fields, output_tool.name)},
        {"role": "user", "content": user_input},
    ]
    config = {"recursion_limit": max(3, int(max_iterations) * 2 + 1)}

    result = {"messages": []}
    published = 0
    try:
        # Retain the last completed state even when the next iteration fails.
        # ainvoke loses it on GraphRecursionError, hiding repeated tool errors.
        async with aclosing(agent.astream(
            {"messages": messages},
            context=ctx,
            config=config,
            stream_mode="values",
        )) as updates:
            async for state in updates:
                if not isinstance(state, dict):
                    continue
                result = state
                if on_trace is not None:
                    trace = _messages_to_trace(list(state.get("messages") or []))
                    for entry in trace[published:]:
                        await on_trace(entry)
                    published = len(trace)
                if "output" in holder:
                    break
    except GraphRecursionError:
        return SubAgentResult(
            status="incomplete",
            output=coerce_to_fields(holder.get("output") or {}, output_fields),
            trace=_messages_to_trace(list(result.get("messages") or [])),
            messages=chatml_messages(list(result.get("messages") or [])),
            error="max_iterations reached" + _tool_diagnostics(list(result.get("messages") or [])),
        )
    except Exception as exc:
        if "output" in holder:
            return SubAgentResult("done", holder["output"], messages=chatml_messages(list(result.get("messages") or [])))
        return SubAgentResult(
            status="error",
            output=coerce_to_fields({}, output_fields),
            error=f"{type(exc).__name__}: {exc}",
            messages=chatml_messages(list(result.get("messages") or [])),
        )

    trace = _messages_to_trace(list(result.get("messages") or []))
    if "output" in holder:
        return SubAgentResult("done", holder["output"], trace=trace, messages=chatml_messages(list(result.get("messages") or [])))
    return SubAgentResult(
        status="incomplete",
        output=coerce_to_fields({}, output_fields),
        trace=trace,
        messages=chatml_messages(list(result.get("messages") or [])),
    )
