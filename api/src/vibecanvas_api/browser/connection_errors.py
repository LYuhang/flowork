"""Public, credential-free diagnostics shared by CDP endpoints and relays.

Only fixed messages cross this boundary. Raw extension/protocol exceptions may
contain page data, URLs or credentials. Close-frame reasons must fit 123 bytes.
"""

INITIALIZATION_FAILED = "Browser initialization failed. Reopen the side panel and check browser-control permission."
INITIALIZATION_TIMEOUT = "Browser extension did not initialize in time. Check its connection and reopen the side panel."
SESSION_CHANGED = "Browser control changed during initialization. Reopen the side panel in the intended Chat."
EXTENSION_DISCONNECTED = "Browser extension disconnected. Reconnect it before retrying."

INITIALIZATION_REASONS = {
    "No controllable active page is available in the side-panel window":
        "No controllable active page. Select a normal webpage in the side-panel window, then retry.",
    "The side-panel browser window is unavailable":
        "Side-panel window unavailable. Reopen the Flowork side panel in the intended browser window.",
    "Playwright initialization is missing its browser-session fence": SESSION_CHANGED,
}
SAFE_REASONS = frozenset({
    *INITIALIZATION_REASONS.values(), INITIALIZATION_FAILED, INITIALIZATION_TIMEOUT,
    SESSION_CHANGED, EXTENSION_DISCONNECTED,
})


class BrowserInitializationError(RuntimeError):
    """An extension initialization refusal, never a dispatched page action."""


def initialization_reason(message: object) -> str:
    return INITIALIZATION_REASONS.get(str(message), INITIALIZATION_FAILED)


def upstream_close(code: int | None, reason: str | None) -> tuple[int, str]:
    """Preserve valid close codes, but never arbitrary upstream error text."""
    valid = code in {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014}
    if not valid and not (isinstance(code, int) and 3000 <= code < 5000):
        code = 1011  # 1005/1006/1015 are observations, not sendable close codes.
    if reason in SAFE_REASONS:
        return code, reason
    return code, {
        1000: "Browser connection closed.",
        1001: EXTENSION_DISCONNECTED,
        1012: "Browser service restarted or the Agent turn ended. Reconnect before retrying.",
        4401: "Browser authorization expired or was revoked. Reconnect the extension.",
        4403: "The originating Agent turn is no longer active.",
        4409: "Browser control is unavailable for this Chat. Check the extension connection and control ownership.",
    }.get(code, EXTENSION_DISCONNECTED)
