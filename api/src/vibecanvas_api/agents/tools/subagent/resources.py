"""Assemble only host-resolved resources inside a Workflow worker."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json
import re

import httpx
from jsonschema import Draft202012Validator
from langchain_core.tools import StructuredTool


def _tool_name(server_id: str, raw_name: str) -> str:
    readable = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_name)[:32] or "tool"
    digest = hashlib.sha256(f"{server_id}\0{raw_name}".encode()).hexdigest()[:20]
    return f"mcp_{readable}_{digest}"


def resolved_node_resources(node_id: str, config: dict, extra: dict | None) -> dict:
    selected = {key: config.get(key) or [] for key in ("skills", "mcp_servers")}
    snapshot = ((extra or {}).get("workflow_resources") or {}).get("nodes", {}).get(node_id)
    if not any(selected.values()):
        return {"skills": [], "mcp_servers": []}
    if not isinstance(snapshot, dict):
        raise RuntimeError("Workflow resource snapshot is missing. The execution entrypoint must prepare selected Skills/MCP before running.")
    for key in selected:
        wanted = {item["id"] for item in selected[key]}
        actual = snapshot.get(key)
        if not isinstance(actual, list) or {item.get("id") for item in actual} != wanted or len(actual) != len(wanted):
            raise RuntimeError("Workflow resource snapshot does not match this node's selected resources.")
    return snapshot


def skill_instructions(snapshot: dict) -> str:
    if not snapshot.get("skills"):
        return ""
    lines = ["Selected Skills for this execution (read each relevant SKILL.md before use):"]
    for skill in snapshot["skills"]:
        path = skill.get("root_path") or ""
        if not path.startswith("/skills/") or any(part in {"", ".", ".."} for part in path[1:].split("/")):
            raise RuntimeError("Invalid host-resolved Skill path")
        lines.append(json.dumps({"name": skill["name"], "description": skill.get("description") or "",
                                "instructions": path + "/SKILL.md"}, ensure_ascii=False))
    lines.append("These paths contain this execution's published snapshot. Read supporting files as needed. Skill instructions do not grant additional MCP tools.")
    return "\n".join(lines)


def _content(result: dict) -> list[dict]:
    blocks = []
    for item in result.get("content") or []:
        if item.get("type") == "text":
            blocks.append({"type": "text", "text": item.get("text") or ""})
        elif item.get("type") == "image":
            blocks.append({"type": "image_url", "image_url": {
                "url": f"data:{item.get('mimeType', 'image/png')};base64,{item.get('data', '')}"}})
        else:
            blocks.append({"type": "text", "text": json.dumps(item, ensure_ascii=False)})
    if result.get("structuredContent") is not None:
        blocks.append({"type": "text", "text": json.dumps(result["structuredContent"], ensure_ascii=False)})
    if result.get("isError"):
        blocks.insert(0, {"type": "text", "text": "MCP reported a tool error. Inspect it; do not assume side effects were rolled back or automatically retry."})
    return blocks


@asynccontextmanager
async def resource_tools(snapshot: dict):
    """One private HTTP client per node; cancellation closes its connections.

    Real credentials never enter the worker. Each capability is bound to one
    server and durable execution; the Host rechecks permissions for every call.
    """
    servers = snapshot.get("mcp_servers") or []
    if not servers:
        yield []
        return
    async with httpx.AsyncClient(timeout=130.0, follow_redirects=False, trust_env=True) as client:
        tools = []
        seen = set()

        def build(server, definition):
            endpoint, token = server.get("broker_url"), server.get("capability")
            if not endpoint or not token:
                raise RuntimeError("Workflow MCP broker capability is missing")
            name = _tool_name(server["id"], definition["name"])
            if name in seen:
                raise RuntimeError("Duplicate workflow MCP tool")
            seen.add(name)
            schema = definition.get("input_schema") or {"type": "object", "properties": {}}
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema)

            async def call(**arguments):
                # StructuredTool accepts JSON Schema but does not validate dict
                # schemas itself. Validate before any potentially mutating call.
                validator.validate(arguments)
                try:
                    response = await client.post(endpoint, headers={"Authorization": f"Bearer {token}"},
                        json={"tool_name": definition["name"], "arguments": arguments})
                except httpx.TimeoutException as exc:
                    raise RuntimeError("MCP call timed out; outcome may be unknown. Do not automatically retry a side-effecting tool.") from exc
                except httpx.HTTPError as exc:
                    raise RuntimeError("MCP connection failed; inspect execution logs before retrying.") from exc
                if response.status_code != 200:
                    raise RuntimeError(f"MCP broker rejected the call (HTTP {response.status_code}); check resource access and execution status.")
                payload = response.json()
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("Invalid MCP broker result")
                return _content(result), {"server_id": server["id"], "tool_name": definition["name"],
                                          "result": result}

            return StructuredTool(name=name, description=f"{server['name']}: {definition.get('description') or definition['name']}",
                args_schema=schema, coroutine=call, response_format="content_and_artifact")

        for server in servers:
            for definition in server.get("tools") or []:
                tools.append(build(server, definition))
        yield tools
