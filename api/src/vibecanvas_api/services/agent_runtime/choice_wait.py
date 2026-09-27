"""Keep one MCP invocation open; lifecycle polling is private, not agent work."""
from __future__ import annotations

import asyncio
import json
import uuid


async def wait_for_choices(gateway, server, arguments):
    call_id = uuid.uuid4().hex
    terminal = False
    async def request(action):
        return await gateway("interaction", server, "render_choices", {
            "action": action, "call_id": call_id, **({"input": arguments} if action == "start" else {})})
    try:
        response = await request("start")
        while response.get("pending"):
            await asyncio.sleep(1)
            response = await request("poll")
        result = response["result"]
        terminal = True
        try:
            await request("ack")
        except Exception:
            # The confirmed result is already durable and received. A failed
            # delivery acknowledgement must not turn it into a failed selection.
            pass
        return {"content": [{"type": "text", "text": json.dumps(result)}],
                "structured_content": result, "is_error": False}
    finally:
        if not terminal:
            # Graceful stop expires immediately. Hard process death is covered
            # by the durable lease; cleanup must never hang shutdown indefinitely.
            cleanup = asyncio.create_task(request("cancel"))
            try:
                await asyncio.wait_for(asyncio.shield(cleanup), timeout=5)
            except (Exception, asyncio.CancelledError):
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
