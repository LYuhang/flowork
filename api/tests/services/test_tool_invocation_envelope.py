from __future__ import annotations

from vibecanvas_api.services.agent_runtime.tool_invocation import (
    finish_tool_invocation,
    start_tool_invocation,
)


def test_runtime_neutral_invocation_classifies_platform_mcp_and_normalizes_json():
    started, clock = start_tool_invocation(
        invocation_id="call-1",
        runtime_type="codex",
        name="knowledge_search",
        arguments='{"query":"release notes","authorization":"Bearer private"}',
        mcp_catalog=[{
            "name": "knowledge",
            "source": "platform",
            "server_id": "platform-knowledge",
            "tools": [{"name": "knowledge_search"}],
        }],
    )

    assert started["schemaVersion"] == 1
    assert started["origin"] == {
        "kind": "platform_mcp",
        "serverId": "platform-knowledge",
        "serverName": "knowledge",
        "serverLabel": "knowledge",
        "toolName": "knowledge_search",
        "qualifiedName": "knowledge_search",
    }
    assert started["capability"] == "knowledge"
    assert started["input"] == {
        "query": "release notes",
        "authorization": "[redacted]",
    }

    finished = finish_tool_invocation(
        started,
        started_monotonic=clock,
        invocation_id="call-1",
        runtime_type="codex",
        name="knowledge_search",
        status="done",
        content="found 2 records",
        artifact={"meta": {"content_type": "application/json"}, "items": [1, 2]},
    )

    assert finished["status"] == "success"
    assert finished["output"]["content"][0] == {
        "type": "text",
        "text": "found 2 records",
    }
    assert finished["output"]["structuredContent"]["items"] == [1, 2]
    assert finished["presentation"]["contentType"] == "application/json"
    assert finished["timing"]["durationMs"] >= 0


def test_error_envelope_is_bounded_and_classifies_native_execution():
    finished = finish_tool_invocation(
        None,
        started_monotonic=None,
        invocation_id="call-2",
        runtime_type="codex",
        name="bash",
        status="failed",
        content="x" * 2500,
        artifact=None,
        native_kind="commandExecution",
    )

    assert finished["origin"] == {"kind": "runtime_native", "provider": "codex"}
    assert finished["nativeKind"] == "commandExecution"
    assert finished["capability"] == "terminal"
    assert finished["risk"] == "execute"
    assert finished["status"] == "error"
    assert len(finished["error"]["message"]) == 2000
