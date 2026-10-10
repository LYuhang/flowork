"""Translate an explicitly submitted user update to one native steering RPC."""
from __future__ import annotations

from .codex_app_server import CodexAppServer, CodexAppServerError
from .protocol import RuntimeSteerInput


async def steer_native_turn(
    client: CodexAppServer,
    update: RuntimeSteerInput,
    *,
    chat_id: str,
    turn_id: str,
    thread_id: str | None,
    native_turn_id: str | None,
    stopping: bool,
) -> dict:
    def result(status: str, error: str | None = None) -> dict:
        return {"input_id": update.input_id, "status": status, "error": error}

    if update.chat_id != chat_id or update.turn_id != turn_id:
        return result("rejected", "turn_mismatch")
    if stopping:
        return result("rejected", "turn_stopping")
    if not thread_id or not native_turn_id:
        return result("rejected", "turn_not_ready")
    try:
        accepted = await client.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": native_turn_id,
                "clientUserMessageId": update.input_id,
                "input": [{"type": "text", "text": update.text}],
            },
            timeout_s=10.0,
        )
    except CodexAppServerError as exc:
        # Only a native RPC error proves rejection. Losing its response does
        # not prove that the input was rejected; never resend it automatically.
        status = "rejected" if exc.code == "codex_app_server_request_failed" else "unknown"
        return result(status, exc.code)
    except (ConnectionError, OSError):
        return result("unknown", "steer_transport_failed")
    if accepted.get("turnId") != native_turn_id:
        return result("unknown", "steer_response_turn_mismatch")
    return result("accepted")
