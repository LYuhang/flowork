"""One-shot in-sandbox MCP manifest probe.

The API host writes ``/run/request.json`` and starts this module inside a fresh
gVisor instance.  User-controlled stdio commands and remote MCP clients are
therefore never instantiated in the API process.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any


def _append_dependency_paths() -> None:
    for path in os.environ.get("VC_SANDBOX_PYTHON_PATHS", "").split(os.pathsep):
        if path and path not in os.sys.path:
            os.sys.path.append(path)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "model_json_schema"):
        return _jsonable(value.model_json_schema())
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    return str(value)


async def _probe(request: dict[str, Any]) -> dict[str, Any]:
    from vibecanvas_api.services.sandbox.mcp_client import mcp_client_session

    connection = request.get("connection")
    timeout_s = float(request.get("timeout_s") or 60.0)
    if not isinstance(connection, dict) or not connection:
        raise ValueError("missing MCP connection config")

    async def list_tools():
        async with mcp_client_session(connection) as session:
            return await session.list_tools()

    listed = await asyncio.wait_for(list_tools(), timeout=timeout_s)
    tools = list(getattr(listed, "tools", []) or [])
    return {
        "status": "ok",
        "tool_count": len(tools),
        "tool_names": [
            {
                "name": str(getattr(tool, "name", "") or ""),
                "description": str(getattr(tool, "description", "") or ""),
                "input_schema": _jsonable(
                    getattr(tool, "inputSchema", None)
                    or getattr(tool, "input_schema", None)
                    or {"type": "object", "properties": {}}
                ),
            }
            for tool in tools
        ],
    }


def main() -> int:
    request_path = Path("/run/request.json")
    result_path = Path("/run/result.json")
    try:
        # In production proxy mode the sandbox has no direct network.  This
        # starts the localhost forward proxy only when VC_EGRESS_* was injected
        # by the host provider; it is a no-op in development host-network mode.
        from vibecanvas_engine.egress_proxy import maybe_start_egress_proxy

        maybe_start_egress_proxy()
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result = asyncio.run(_probe(request))
        exit_code = 0
    except asyncio.TimeoutError:
        timeout_s = 0.0
        try:
            timeout_s = float(
                json.loads(request_path.read_text(encoding="utf-8")).get("timeout_s")
                or 0.0
            )
        except Exception:
            pass
        result = {
            "status": f"error: handshake timed out after {timeout_s:g}s",
            "tool_count": None,
            "tool_names": None,
        }
        exit_code = 2
    except BaseException as exc:
        message = str(exc).strip()
        result = {
            "status": (
                f"error: {type(exc).__name__}: {message}"
                if message
                else f"error: {type(exc).__name__}"
            ),
            "tool_count": None,
            "tool_names": None,
        }
        exit_code = 1

    result_path.write_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
