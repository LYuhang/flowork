"""Host-only fresh authorization for sandbox Browser CLI commands."""

import re

from vibecanvas_api.browser.registry import registry
from vibecanvas_api.config import config
from vibecanvas_api.flowork_cli.browser_cli import validate
from vibecanvas_api.services.agent_resources.capability import verify_agent_capability
from vibecanvas_api.services.agent_resources.context import resolve_context
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope


async def authorize_browser_cli(*, operation, arguments, token, endpoint):
    validate(operation, arguments)
    capability = verify_agent_capability(token, secret=config.signing_secret, server="browser")
    if capability is None:
        raise PermissionError("Invalid or expired Browser CLI identity")
    # Revalidates membership, Chat execute permission, originating active run,
    # session generation and runtime/workspace identity on every command/renewal.
    await resolve_context(capability)
    transport = registry.find_for_session(capability.organization_id, capability.user_id, capability.session_id)
    if transport is None:
        return {"error": "browser_disconnected", "message": "The authorized browser extension is not connected.",
                "hint": "Open the Flowork side panel in the intended browser and reconnect. No other browser was selected."}
    async with session_scope(tenant_id=capability.organization_id, user_id=capability.user_id) as session:
        binding = await ChatRepo(session, capability.user_id).get_browser_binding(capability.chat_id)
    if (not binding or binding.get("status") not in {"attaching", "attached"}
            or not binding.get("browser_session_id") or int(binding.get("browser_session_generation") or 0) <= 0):
        return {"error": "browser_lease_missing", "message": "This Chat does not have a live browser-control lease.",
                "hint": "Send the browser task from the extension side panel; do not guess another tab or browser."}
    # This material travels only on the private Runtime bus. Never put it in
    # shell stdout, tool arguments, product events or durable Agent messages.
    return {"endpoint": endpoint, "bearer": token,
            "fence": [transport, str(binding["browser_session_id"]), int(binding["browser_session_generation"])]}


async def commit_browser_artifacts(*, operation, arguments, artifacts, token):
    """Acknowledge exact files in the originating Chat, without RPC file bytes."""
    from vibecanvas_api.services.sandbox.coordinator import get_sandbox_coordinator

    validate(operation, arguments)
    # Cookie export is intentionally NOT an ordinary shareable VFS artifact.
    if operation == "browser.cookie-export":
        raise PermissionError("Cookie files cannot be committed as public browser artifacts")
    capability = verify_agent_capability(token, secret=config.signing_secret, server="browser")
    if capability is None:
        raise PermissionError("Invalid or expired Browser CLI identity")
    await resolve_context(capability)
    if not isinstance(artifacts, list):
        raise ValueError("Invalid browser artifact list")
    checked = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("Invalid browser artifact")
        path, digest, size = artifact.get("file"), artifact.get("sha256"), artifact.get("bytes")
        if (not isinstance(path, str) or not path.startswith("/") or "\x00" in path
                or any(part in {".", ".."} for part in path.split("/"))
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or type(size) is not int or size < 0):
            raise ValueError("Invalid browser artifact path, hash or byte count")
        checked.append({"file": path, "sha256": digest, "bytes": size})
    sandbox = await get_sandbox_coordinator().get_loaded_session(
        capability.organization_id, capability.workspace_scope_id,
    )
    if sandbox is None:
        return {"error": "artifact_persistence_failed", "message": "The originating Chat sandbox is no longer available.", "artifacts": []}
    committed = []
    for artifact in checked:
        path = artifact["file"]
        if not path.startswith(("/data/", "/memory/", "/logs/", "/chats/")):
            committed.append({**artifact, "persistence": "sandbox",
                              "warning": "This path is outside the durable Chat workspace. Use /data for downloads that must survive sandbox release."})
            continue
        persisted = await sandbox.sync_workspace_path(
            path, expected_sha256=artifact["sha256"], expected_bytes=artifact["bytes"],
        )
        if not persisted:
            return {"error": "artifact_persistence_failed",
                    "message": "The local file was saved, but its exact bytes were not acknowledged by durable storage.",
                    "hint": "Do not repeat the browser action. Inspect the saved file and retry persistence or report the storage failure.",
                    "artifacts": committed, "unconfirmed_artifact": artifact}
        committed.append({**artifact, "persistence": "durable"})
    return {"artifacts": committed}
