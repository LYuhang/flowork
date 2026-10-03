import base64
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from PIL import Image

from vibecanvas_api.agents.tools.subagent.images import model_messages_with_images


def test_image_projection_keeps_parallel_results_adjacent_and_does_not_mutate():
    calls = [{"name": "read_images", "args": {}, "id": name, "type": "tool_call"} for name in ("a", "b")]
    block = {"type": "image", "base64": "encoded", "mime_type": "image/png"}
    original = [
        HumanMessage(content="Read both images"),
        AIMessage(content="", tool_calls=calls),
        ToolMessage(content=[{"type": "text", "text": "Image A"}, block], tool_call_id="a"),
        ToolMessage(content=[{"type": "text", "text": "Image B"}, block], tool_call_id="b"),
    ]
    projected = model_messages_with_images(original)
    assert [m.type for m in projected] == ["human", "ai", "tool", "tool", "human"]
    assert [m.tool_call_id for m in projected if m.type == "tool"] == ["a", "b"]
    assert sum(b["type"] == "image" for b in projected[-1].content) == 2
    assert original[2].content[1] == block
    assert model_messages_with_images(original) == projected
    assert model_messages_with_images(projected) == projected


@pytest.mark.asyncio
async def test_real_agent_image_tool_reaches_chat_request_without_text_base64(monkeypatch, tmp_path):
    """Real tool, graph, middleware and model serializer; only HTTP is mocked."""
    from langchain_openai import ChatOpenAI
    from vibecanvas_api.agents.tools import workspace_fs
    from vibecanvas_api.agents.tools.subagent.core import run_bounded_agent
    from vibecanvas_api.agents.tools.subagent.toolset import build_agent_subagent_tools

    monkeypatch.delenv("VIBECANVAS_AGENT_RUNTIME_IN_SANDBOX", raising=False)
    monkeypatch.setattr(workspace_fs, "_WORKSPACE_ROOTS", (str(tmp_path),))
    path = tmp_path / "chart.png"
    Image.new("RGB", (20, 30), color="blue").save(path)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    requests = []

    async def reply(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["tool_choice"] == "required"
        if len(requests) == 1:
            name, args = "read_images", {"paths": [str(path)]}
        else:
            assert len(requests) == 2
            messages = payload["messages"]
            assert messages[-2]["role"] == "tool"
            assert isinstance(messages[-2]["content"], str)
            assert messages[-1]["role"] == "user"
            images = [b for b in messages[-1]["content"] if b["type"] == "image_url"]
            assert len(images) == 1
            assert images[0]["image_url"]["url"] == "data:image/png;base64," + encoded
            name, args = "set_output", {"answer": "blue"}
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "test-vision",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "", "tool_calls": [{
                    "id": f"call_{len(requests)}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }],
            }}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        model = ChatOpenAI(
            model="test-vision", api_key="test-placeholder", base_url="https://test.invalid/v1",
            http_async_client=client, use_responses_api=False, max_retries=0,
        )
        result = await run_bounded_agent(
            model=model, tools=build_agent_subagent_tools(working_dir=str(tmp_path)),
            system_prompt="Describe the image", user_input=str(path),
            output_fields={"answer": {"type": "string"}}, max_iterations=3,
        )
    assert result.status == "done", result.error
    assert result.output == {"answer": "blue"}
    assert len(requests) == 2
    assert encoded not in json.dumps(result.trace)
    assert any("Loaded 1 image(s)" in entry["text"] for entry in result.trace)
