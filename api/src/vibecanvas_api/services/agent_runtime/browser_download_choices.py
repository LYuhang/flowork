"""Host-owned download candidates. Agent-provided labels/IDs are never authority.

Registration is called ONLY on correlated extension responses, not on an Agent
RPC. Metadata is encrypted with the Chat key. Every use rechecks the active turn
and browser fence; stopping a turn makes historical rows unusable immediately.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from vibecanvas_api.browser.cluster_registry import registry
from vibecanvas_api.security.content_encryption import content_encryption_service
from vibecanvas_api.services.agent_resources.capability import AgentCapability
from vibecanvas_api.services.platform_mcp.interactive_tools.render_choices import ChoicesInput
from vibecanvas_api.services.agent_resources.context import resolve_context
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.hitl_repo import HitlRepo


class DownloadCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: str = Field(pattern="^ready$")
    candidate_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1)
    bytes: int = Field(ge=0)
    source: str


@dataclass(frozen=True)
class DownloadScope:
    transport_id: str
    browser_session_id: str
    browser_generation: int


class BrowserTransportUnavailable(ConnectionError):
    """May recover within the existing choice lease; never permits file IO."""


class SelectionRequired(PermissionError):
    """Recoverable: preserve the capture while the user chooses a file."""


async def current_scope(session, cap: AgentCapability) -> DownloadScope:
    await resolve_context(cap)
    transport = await registry.find_for_session(cap.organization_id, cap.user_id, cap.session_id)
    binding = await ChatRepo(session, cap.user_id).get_browser_binding(cap.chat_id)
    if (not binding or binding.get("status") not in {"attaching", "attached", "lost"}
            or not binding.get("browser_session_id") or int(binding.get("browser_session_generation") or 0) <= 0):
        raise PermissionError("The originating browser session is no longer active.")
    if not transport or binding.get("status") == "lost":
        raise BrowserTransportUnavailable("The browser transport is reconnecting. No file transfer is authorized.")
    return DownloadScope(transport, str(binding["browser_session_id"]), int(binding["browser_session_generation"]))


def _params(cap, scope):
    return {"tenant": cap.tenant_id, "user": cap.user_id, "chat": cap.chat_id, "run": cap.turn_id,
            "runtime": cap.runtime_session_id, "transport": scope.transport_id,
            "browser": scope.browser_session_id, "generation": scope.browser_generation}


async def register_candidates(session, cap, scope: DownloadScope, *, tab_id: int, capture_id: str, candidates: list) -> str:
    """Persist ONLY the metadata returned by the trusted extension controller."""
    if scope != await current_scope(session, cap):
        raise PermissionError("The browser changed before download registration.")
    if type(tab_id) is not int or tab_id < 0 or not capture_id or len(capture_id) > 128:
        raise ValueError("Invalid browser download capture.")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 100:
        raise ValueError("A download capture must contain between 1 and 100 completed candidates.")
    checked = [DownloadCandidate.model_validate(item).model_dump() for item in candidates]
    checked.sort(key=lambda item: item["candidate_id"])
    if len({item["candidate_id"] for item in checked}) != len(checked):
        raise ValueError("Duplicate registered download candidate.")
    params = {**_params(cap, scope), "tab": tab_id, "capture": capture_id}
    fingerprint = hashlib.sha256(json.dumps([params, checked], sort_keys=True).encode()).hexdigest()
    set_id = "downloads_" + fingerprint
    params["id"] = set_id
    # Serialize snapshots of one capture: a changed candidate list revokes its
    # older choices rather than silently transferring a different file.
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:lock, 0))"),
                          {"lock": f"downloads:{cap.tenant_id}:{cap.turn_id}:{capture_id}"})
    existing = (await session.execute(text("SELECT revoked_at FROM browser_download_choices WHERE choice_set_id=:id"), params)).first()
    if existing:
        if existing.revoked_at is not None:
            raise PermissionError("This download candidate snapshot has expired. Inspect the current capture again.")
        return set_id
    encrypted = await content_encryption_service().encrypt_json(session,
        tenant_id=cap.tenant_id, resource_type="chat", resource_id=cap.chat_id,
        purpose="browser_download_candidates", record_id=set_id, value={"candidates": checked})
    await session.execute(text("""UPDATE browser_download_choices SET revoked_at=now()
        WHERE tenant_id=CAST(:tenant AS uuid) AND run_id=:run AND capture_id=:capture AND revoked_at IS NULL"""), params)
    await session.execute(text("""INSERT INTO browser_download_choices
        (choice_set_id,tenant_id,user_id,chat_id,run_id,runtime_session_id,transport_id,browser_session_id,
         browser_generation,capture_id,tab_id,private_ciphertext,private_nonce,private_key_id)
        VALUES (:id,CAST(:tenant AS uuid),CAST(:user AS uuid),:chat,:run,:runtime,:transport,:browser,
                :generation,:capture,:tab,:cipher,:nonce,CAST(:key AS uuid))"""),
        {**params, "cipher": encrypted.ciphertext, "nonce": encrypted.nonce, "key": encrypted.key_id})
    return set_id


async def load_candidates(session, cap, set_id: str):
    scope = await current_scope(session, cap)
    row = (await session.execute(text("""SELECT * FROM browser_download_choices
        WHERE choice_set_id=:id AND tenant_id=CAST(:tenant AS uuid) AND user_id=CAST(:user AS uuid)
          AND chat_id=:chat AND run_id=:run AND runtime_session_id=:runtime
          AND transport_id=:transport AND browser_session_id=:browser AND browser_generation=:generation
          AND revoked_at IS NULL FOR UPDATE"""), {**_params(cap, scope), "id": set_id})).mappings().first()
    if row is None:
        raise PermissionError("Download choices are unavailable in this active browser turn. Do not guess or re-download the file.")
    value = await content_encryption_service().decrypt_json(session,
        tenant_id=cap.tenant_id, resource_type="chat", resource_id=cap.chat_id,
        purpose="browser_download_candidates", record_id=set_id,
        key_id=row["private_key_id"], ciphertext=row["private_ciphertext"], nonce=row["private_nonce"])
    candidates = [DownloadCandidate.model_validate(item) for item in value["candidates"]]
    return row, candidates


async def resolve_choices(session, cap, request, hitl_id: str) -> ChoicesInput:
    row, candidates = await load_candidates(session, cap, request.choice_set_id)
    if row["hitl_request_id"] and row["hitl_request_id"] != hitl_id:
        previous = await HitlRepo(session).get_request(row["hitl_request_id"])
        if previous and previous.status in {"pending", "submitted"}:
            raise ValueError("This download already has a choice call. Wait on the original call or use its confirmed selection.")
    if request.multiple:
        raise ValueError("Choose exactly one download per transfer; multiple must be false.")
    return ChoicesInput(title=request.title, description=request.description, options=[
        {"id": item.candidate_id, "label": item.name,
         "description": f"{item.bytes} bytes · {item.source}"} for item in candidates])


async def bind_choice(session, set_id: str, hitl_id: str):
    # Caller holds the row lock acquired by resolve_choices in this transaction.
    await session.execute(text("UPDATE browser_download_choices SET hitl_request_id=:hitl WHERE choice_set_id=:id"),
                          {"id": set_id, "hitl": hitl_id})


async def require_selection(session, cap, set_id: str, candidate_id: str):
    """Selection proof only. Callers MUST separately obtain transfer approval."""
    row, candidates = await load_candidates(session, cap, set_id)
    candidate = next((item for item in candidates if item.candidate_id == candidate_id), None)
    if candidate is None:
        raise PermissionError("The file is not a registered candidate in this capture.")
    if len(candidates) > 1:
        hitl = await HitlRepo(session).get_request(row["hitl_request_id"]) if row["hitl_request_id"] else None
        if (not hitl or hitl.chat_id != cap.chat_id or hitl.run_id != cap.turn_id or hitl.status != "submitted"
                or hitl.runtime_correlation_json.get("source") != "render_choices"
                or hitl.interaction_result_json.get("selected_ids") != [candidate_id]):
            raise SelectionRequired("User selection is required. Call render_choices with this choice_set_id and wait for confirmation.")
    return row, candidate
