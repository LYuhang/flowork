"""Host-side Codex Runtime adapter.

Codex app-server protocol objects are translated inside the sandbox.  This
class intentionally exposes only the SDK-neutral Runtime protocol to the API.
"""

from vibecanvas_api.services.agent_runtime.sandbox_runtime import SandboxProcessRuntime


class CodexSandboxRuntime(SandboxProcessRuntime):
    """Codex marker used by the backend Runtime registry."""
