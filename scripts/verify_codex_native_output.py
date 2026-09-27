#!/usr/bin/env python3
"""Isolate native Codex shell-output loss without a model or a real browser.

Run with the API environment and PYTHONPATH=api/src. A loopback Responses stub
instructs the installed app-server to run fixed synthetic commands. Compare
model-facing results with commandExecution events; never replay application
operations. Exit 1 means missing/mismatched output, not a successful repair.
This diagnostic is NOT Browser CLI live acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
import threading

from vibecanvas_api.services.agent_runtime.codex_app_server import CodexAppServer


def command_arguments(marker: str, *, early: bool, directory: str) -> dict:
    # Identical bounded lifetime; only output timing differs. No actual resource
    # mutations, inherited credentials, or model-generated shell are involved.
    emit = f"printf '%s\\n' {shlex.quote(marker)}"
    command = f"{emit}; sleep 0.8" if early else f"sleep 0.8; {emit}"
    return {"cmd": command, "login": False, "workdir": directory,
            "yield_time_ms": 1000, "max_output_tokens": 1000}


def response_events(item: dict | None, number: int) -> bytes:
    response = {"id": f"native-output-response-{number}"}
    events = [{"type": "response.created", "response": response}]
    if item is not None:
        events.append({"type": "response.output_item.done", "item": item})
    events.append({"type": "response.completed", "response": {
        **response, "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }})
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


class ResponseFixture(ThreadingHTTPServer):
    def __init__(self, items: list[dict]):
        super().__init__(("127.0.0.1", 0), ResponseHandler)
        self.items = items
        self.requests: list[dict] = []
        self.errors: list[str] = []


class ResponseHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        fixture = self.server
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        if self.headers.get("Content-Encoding"):
            fixture.errors.append("Unexpected compressed fixture request")
            self.send_error(415)
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        fixture.requests.append(request)
        index = len(fixture.requests) - 1
        if index > len(fixture.items):
            fixture.errors.append("Unexpected extra model request; commands were not replayed")
            self.send_error(409)
            return
        body = response_events(fixture.items[index] if index < len(fixture.items) else None, index)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def verify(executable: str, *, code_mode: bool, background: bool, directory: str) -> dict:
    markers = [f"FLOWORK_NATIVE_OUTPUT_{phase}_{index}" for phase in ("EARLY", "LATE")
               for index in range(3)]
    calls = []
    for marker in markers:
        arguments = command_arguments(marker, early="_EARLY_" in marker, directory=directory)
        code = f"text(await tools.exec_command({json.dumps(arguments)}));"
        if background:
            emit = f"printf '%s\\n' {shlex.quote(marker)}"
            finish = f"printf '%s\\n' {shlex.quote(marker + '_END')}"
            arguments["cmd"] = (f"{emit}; sleep 1.6; {finish}" if "_EARLY_" in marker
                                else f"sleep 1.6; {emit}; {finish}")
            arguments["yield_time_ms"] = 250
            code = f"""let result = await tools.exec_command({json.dumps(arguments)});
text(result);
if (!result.session_id) throw new Error('Expected a background session');
while (result.session_id) {{
  result = await tools.write_stdin({{session_id: result.session_id, chars: '', yield_time_ms: 1000}});
  text(result);
}}"""
        if code_mode:
            calls.append({"type": "custom_tool_call", "call_id": marker,
                          "name": "exec", "input": code})
        else:
            calls.append({"type": "function_call", "call_id": marker,
                          "name": "exec_command", "arguments": json.dumps(arguments)})
    fixture = ResponseFixture(calls)
    server_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    server_thread.start()
    isolated_home = Path(directory) / ".codex"
    isolated_home.mkdir(mode=0o700)
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
    env.update(CODEX_HOME=str(isolated_home), CODEX_SQLITE_HOME=str(isolated_home))
    client = CodexAppServer(executable=executable, env=env, cwd=directory, experimental=True)
    started: dict[str, dict] = {}
    completed: dict[str, dict] = {}
    deltas: dict[str, str] = {}
    turn_status = None
    runtime_errors = []
    try:
        await client.start()
        thread = await client.request("thread/start", {
            "model": "gpt-5.4", "modelProvider": "output_fixture", "cwd": directory,
            "approvalPolicy": "never", "sandbox": "danger-full-access",
            "config": {
                "web_search": "disabled", "check_for_update_on_startup": False,
                "features": {"unified_exec": True, "code_mode": {"enabled": code_mode},
                             "enable_request_compression": False, "shell_snapshot": False},
                "model_providers": {"output_fixture": {
                    "name": "Loopback output diagnostic (no model)",
                    "base_url": f"http://127.0.0.1:{fixture.server_port}/v1",
                    "wire_api": "responses", "requires_openai_auth": False,
                    "supports_websockets": False,
                }},
            },
        }, timeout_s=20)
        await client.request("turn/start", {
            "threadId": thread["thread"]["id"], "input": [
                {"type": "text", "text": "Run the fixed synthetic output diagnostic."}],
        }, timeout_s=20)
        async with asyncio.timeout(45):
            async for notification in client.messages():
                method = notification.get("method")
                params = notification.get("params", {})
                if method in {"item/started", "item/completed"}:
                    item = params.get("item", {})
                    if item.get("type") == "commandExecution":
                        (started if method == "item/started" else completed)[item["id"]] = item
                elif method == "item/commandExecution/outputDelta":
                    item_id = params["itemId"]
                    deltas[item_id] = deltas.get(item_id, "") + params["delta"]
                elif method == "error":
                    runtime_errors.append(params.get("error", {}).get("message", "Native error"))
                elif method == "turn/completed":
                    turn_status = params["turn"]["status"]
                    break
    finally:
        await client.close()
        await asyncio.to_thread(fixture.shutdown)
        fixture.server_close()
        server_thread.join(timeout=2)

    # Only inspect returned tool outputs, not input prompts/arguments containing
    # the same markers. The deterministic one-call-per-response ordering binds
    # a nested Code Mode result to its own parent, never to another invocation.
    model_outputs = {}
    for request in fixture.requests:
        for item in request.get("input", []):
            if item.get("type") in {"function_call_output", "custom_tool_call_output"}:
                model_outputs[item.get("call_id")] = json.dumps(item.get("output"))
    comparisons = []
    for marker in markers:
        matches = [(item_id, item) for item_id, item in started.items()
                   if marker in item.get("command", "")]
        item_id = matches[0][0] if len(matches) == 1 else None
        end = completed.get(item_id, {})
        aggregate = end.get("aggregatedOutput")
        expected = [marker, marker + "_END"] if background else [marker]
        comparisons.append({
            "marker": marker, "item_id": item_id, "matched_starts": len(matches),
            "completed": item_id in completed, "exit_code": end.get("exitCode"),
            "model_has_output": all(re.search(re.escape(part) + r"(?![A-Za-z0-9_])",
                                               model_outputs.get(marker, "")) is not None for part in expected),
            "aggregate_has_output": isinstance(aggregate, str) and all(part + "\n" in aggregate for part in expected),
            "delta_has_output": all(part + "\n" in deltas.get(item_id, "") for part in expected),
        })
    passed = (turn_status == "completed" and not fixture.errors and not runtime_errors
              and all(row["matched_starts"] == 1 and row["completed"] and row["exit_code"] == 0
                      and row["model_has_output"] and row["aggregate_has_output"]
                      and row["delta_has_output"] for row in comparisons))
    return {"passed": passed, "code_mode": code_mode, "background": background, "turn_status": turn_status,
            "model_requests": len(fixture.requests), "fixture_errors": fixture.errors,
            "runtime_errors": runtime_errors, "comparisons": comparisons,
            "missing_model_result_previews": {
                row["marker"]: model_outputs.get(row["marker"], "<no tool result>")[:2000]
                for row in comparisons if not row["model_has_output"]
            }}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--code_mode", action="store_true")
    parser.add_argument("--background", action="store_true",
                        help="With --code_mode, verify initial output plus write_stdin completion")
    args = parser.parse_args()
    if not args.codex:
        parser.error("Codex is not installed; specify --codex /absolute/path")
    if args.background and not args.code_mode:
        parser.error("--background requires --code_mode")
    with tempfile.TemporaryDirectory(prefix="flowork-native-output-") as directory:
        result = asyncio.run(verify(args.codex, code_mode=args.code_mode,
                                   background=args.background, directory=directory))
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
