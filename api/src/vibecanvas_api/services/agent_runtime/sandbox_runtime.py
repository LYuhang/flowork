"""Host transport shared by sandboxed Agent Runtime adapters."""

from __future__ import annotations

from typing import AsyncIterator

from vibecanvas_api.services.agent_runtime.protocol import (
    RuntimeControlMessage,
    RuntimeEvent,
    RuntimeOpenRequest,
    RuntimeSession,
    RuntimeTurnRequest,
)


class SandboxProcessRuntime:
    """Translate the stable Runtime interface to a sandbox session stream.

    Runtime SDKs and their dependencies live in the sandbox image. The host
    API sees only this protocol and never imports a vendor SDK.
    """

    def __init__(self, sandbox_session) -> None:
        self._sandbox_session = sandbox_session
        self._session: RuntimeSession | None = None

    async def open(self, request: RuntimeOpenRequest) -> RuntimeSession:
        self._session = RuntimeSession(
            runtime_type=request.runtime_type,
            runtime_session_id=request.runtime_session_id,
            state_ref=request.state_ref,
            runtime_version=request.runtime_version,
        )
        return self._session

    async def run_turn(
        self, request: RuntimeTurnRequest
    ) -> AsyncIterator[RuntimeEvent]:
        if self._session is None:
            raise RuntimeError("runtime must be opened before run_turn")
        if request.runtime_session_id != self._session.runtime_session_id:
            raise ValueError("turn runtime_session_id does not match open session")
        async for raw in self._sandbox_session.run_agent_runtime_stream(
            request.model_dump(mode="json")
        ):
            yield RuntimeEvent.model_validate(raw)

    async def respond(self, response: RuntimeControlMessage) -> None:
        if self._session is None:
            raise RuntimeError("runtime must be opened before respond")
        if response.chat_id == "" or response.turn_id == "":
            raise ValueError("runtime control requires chat_id and turn_id")
        await self._sandbox_session.send_agent_runtime_control(
            response.turn_id,
            response.model_dump(mode="json"),
        )

    async def cancel(self, turn_id: str) -> bool:
        return await self._sandbox_session.cancel_agent_runtime(turn_id)

    async def close(self) -> None:
        self._session = None


__all__ = ["SandboxProcessRuntime"]
