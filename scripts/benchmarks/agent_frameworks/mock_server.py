"""Local deterministic Chat Completions + HTTP tool fixture; no external LLM."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time


def tool_value(name, value):
    return value + 1 if name == "lookup" else value * 2


def completion(body):
    messages = body["messages"]
    text = " ".join(str(m.get("content")) for m in messages if m["role"] == "user")
    case_id = int(re.search(r"case_id=(\d+)", text).group(1))
    # Count actual completed tool messages, never invent a completed tool step.
    results = [m for m in messages if m["role"] == "tool"]
    # smolagents represents tool observations as user content instead of
    # Chat Completions tool-role messages. Accept that documented encoding,
    # while still validating every returned tool value and exact round count.
    if not results:
        for message in messages:
            content = message.get("content")
            blocks = content if isinstance(content, list) else [{"text": content}]
            for block in blocks:
                if isinstance(block, dict) and str(block.get("text", "")).startswith("Observation:\n"):
                    results.append({"content": block["text"]})
    names = [t["function"]["name"] for t in body.get("tools", [])]
    if len(results) < 2:
        name = "lookup" if not results else "scale"
        value = case_id if not results else case_id + 1
        args = {"value": value}
    else:
        name = "set_output" if "set_output" in names else "final_answer"
        answer = {"answer": 2 * (case_id + 1), "case_id": case_id}
        args = answer if name == "set_output" else {"answer": answer}
    if name not in names:
        raise ValueError(f"expected tool {name}, received {names}")
    # Verify the tool response carries the expected value, rather than timing
    # an agent which silently ignores tool execution failures.
    for index, result in enumerate(results[:2]):
        expected = case_id + 1 if index == 0 else 2 * (case_id + 1)
        content = str(result.get("content"))
        if not re.search(rf"(?<!\d){expected}(?!\d)", content):
            raise ValueError("tool result did not contain the expected value")
    return {
        "id": f"chatcmpl-bench-{case_id}-{len(results)}", "object": "chat.completion",
        "created": int(time.time()), "model": "benchmark-model",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{case_id}_{len(results)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }],
        }}],
        "usage": {"prompt_tokens": 64, "completion_tokens": 16, "total_tokens": 80},
    }


async def serve(port, model_delay, tool_delay):
    counts = {"model": 0, "tool": 0, "invalid": 0}

    async def handle(reader, writer):
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 60)
                except (asyncio.IncompleteReadError, TimeoutError):
                    break
                lines = raw.decode().split("\r\n")
                method, path, _ = lines[0].split()
                headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
                length = next((int(v) for k, v in headers.items() if k.lower() == "content-length"), 0)
                body = json.loads(await reader.readexactly(length)) if length else {}
                status = 200
                try:
                    if path == "/v1/chat/completions":
                        if body.get("stream"):
                            raise ValueError("benchmark requires non-streaming requests")
                        counts["model"] += 1
                        await asyncio.sleep(model_delay)
                        payload = completion(body)
                    elif path == "/tool":
                        counts["tool"] += 1
                        await asyncio.sleep(tool_delay)
                        payload = {"value": tool_value(body["name"], body["value"])}
                    elif path == "/stats":
                        payload = dict(counts)
                        if method == "DELETE":
                            counts.update(model=0, tool=0, invalid=0)
                    else:
                        status, payload = 404, {"error": "unknown route"}
                except Exception as exc:
                    counts["invalid"] += 1
                    status, payload = 400, {"error": {"message": str(exc), "type": "fixture_error"}}
                data = json.dumps(payload).encode()
                writer.write(f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: keep-alive\r\n\r\n".encode() + data)
                await writer.drain()
        except (ConnectionError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    print(json.dumps({"port": server.sockets[0].getsockname()[1]}), flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--model-delay", type=float, default=.1)
    parser.add_argument("--tool-delay", type=float, default=.01)
    args = parser.parse_args()
    asyncio.run(serve(args.port, args.model_delay, args.tool_delay))
