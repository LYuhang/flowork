"""Host-only fresh authorization for sandbox Browser CLI commands."""

import re

from vibecanvas_api.browser.cluster_registry import registry
from vibecanvas_api.browser.instance_relay import RelayUnavailable
from redis.exceptions import RedisError
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.config import config
from vibecanvas_api.flowork_cli.browser_cli import validate
from vibecanvas_api.services.agent_resources.capability import verify_agent_capability
from vibecanvas_api.services.agent_resources.context import resolve_context


async def authorize_browser_cli(*, operation, arguments, token, endpoint, expected_fence=None):
    validate(operation, arguments)
    capability = verify_agent_capability(token, secret=config.signing_secret, server="browser")
    if capability is None:
        raise PermissionError("Invalid or expired Browser CLI identity")
    # Revalidates membership, Chat execute permission, originating active run,
    # session generation and runtime/workspace identity on every command/renewal.
    await resolve_context(capability)
    try:
        transport = await registry.find_for_session(capability.organization_id, capability.user_id, capability.session_id)
    except (RelayUnavailable, RedisError, OSError, TimeoutError):
        return {"error": "browser_routing_unavailable",
                "message": "Browser routing could not be verified. This command was not dispatched.",
                "hint": "Check service availability before issuing another command."}
    if transport is None:
        return {"error": "browser_disconnected", "message": "The authorized browser extension is not connected.",
                "hint": "If the side panel is open, let its transport reconnect before an explicit new command. Do not replay previous actions. Open it only if it is closed. No other browser was selected."}
    if expected_fence is not None:
        # A running command's periodic authorization must never reacquire
        # control after the user detached debugger or another generation won.
        async with session_scope(tenant_id=capability.organization_id, user_id=capability.user_id) as session:
            binding = await ChatRepo(session, capability.user_id).get_browser_binding(capability.chat_id)
        current = [transport, binding.get("browser_session_id"), binding.get("browser_session_generation")] if binding else None
        if current != expected_fence or not binding or binding.get("status") not in {"attaching", "attached"}:
            return {"error": "browser_control_released", "message": "Browser control was released or replaced. The running browser command was stopped."}
        return {"endpoint": endpoint, "bearer": token, "fence": current}
    # Browser ownership is acquired only when an actual browser command runs.
    # Ordinary conversation must not reserve the browser or fail on another Chat's lease.
    from vibecanvas_api.browser.session_control import (
        BrowserSessionControlError, reserve_sidepanel_browser_session,
    )
    try:
        lease = await reserve_sidepanel_browser_session(
            tenant_id=capability.organization_id, user_id=capability.user_id,
            chat_id=capability.chat_id,
        )
    except BrowserSessionControlError as exc:
        return {"error": exc.code, "message": str(exc),
                "hint": ("Continue without browser control, or wait for the user to release the other Chat's control before trying again. Do not take over another session."
                         if exc.code == "browser_busy" else "Check the extension connection and the reported error before retrying the browser command.")}
    # This material travels only on the private Runtime bus. Never put it in
    # shell stdout, tool arguments, product events or durable Agent messages.
    return {"endpoint": endpoint, "bearer": token,
            "fence": [transport, lease.browser_session_id, lease.session_generation]}


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
