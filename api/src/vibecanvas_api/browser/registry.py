"""In-process live-browser transport registry."""
from __future__ import annotations
from typing import Awaitable, Callable

Sender = Callable[[str], Awaitable[None]]


class TransportSendFailed(Exception):
    """A live sender existed, but the WebSocket write did not complete cleanly.

    Unlike a missing sender, this is an uncertain-delivery boundary: the peer may
    have received some or all of the frame before the transport raised.
    """


class TransportRegistry:
    def __init__(self) -> None:
        self._senders: dict[str, Sender] = {}
        self._session_ids: dict[str, str] = {}

    def register(
        self,
        transport_id: str,
        send: Sender,
        *,
        session_id: str = "",
    ) -> None:
        self._senders[transport_id] = send  # replace any prior connection
        if session_id:
            self._session_ids[transport_id] = str(session_id)
        else:
            self._session_ids.pop(transport_id, None)

    def unregister(self, transport_id: str, sender: Sender | None = None) -> bool:
        # A reconnect replaces the sender before the old WebSocket's ``finally``
        # runs.  The old connection must not unregister the newer one.
        if sender is not None and self._senders.get(transport_id) is not sender:
            return False
        removed = self._senders.pop(transport_id, None) is not None
        if removed:
            self._session_ids.pop(transport_id, None)
        return removed

    def is_connected(self, transport_id: str) -> bool:
        return transport_id in self._senders

    def user_transports(self, tenant_id: str, user_id: str) -> list[str]:
        """Connected transports for one authenticated user.

        Internal ids are ``<tenant>:<user>:<browser-profile>``. Browser topology
        never crosses the HTTP Turn or Agent Context boundary.
        """
        prefix = f"{tenant_id}:{user_id}:"
        return [tid for tid in self._senders if tid.startswith(prefix)]

    def find_for_user(self, tenant_id: str, user_id: str) -> str | None:
        """Return the user's sole V1 browser-control transport.

        Never guess when multiple browser entities are connected: choosing the
        newest one could execute a write in the wrong real browser. V1 exposes
        exactly one; ambiguity therefore fails closed as unavailable.
        """
        matches = self.user_transports(tenant_id, user_id)
        return matches[0] if len(matches) == 1 else None

    def find_for_session(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> str | None:
        """Resolve the exact extension transport for an authenticated Session.

        Every browser profile redeems its own derived Extension Session. The
        Platform MCP capability carries that same non-secret Session UUID, so
        multiple connected browsers for one account remain deterministic
        without exposing browser topology to the Agent or HTTP Turn body.
        """
        matches = [
            transport_id
            for transport_id in self.user_transports(tenant_id, user_id)
            if self._session_ids.get(transport_id) == str(session_id)
        ]
        return matches[0] if len(matches) == 1 else None

    async def send_to(self, transport_id: str, raw: str) -> bool:
        send = self._senders.get(transport_id)
        if send is None:
            return False
        try:
            await send(raw)
            return True
        except Exception as exc:
            # A replacement may have registered while this send was pending.
            # Remove only the failed sender, never its replacement.
            self.unregister(transport_id, send)
            raise TransportSendFailed(f"transport write failed for {transport_id}") from exc

registry = TransportRegistry()
