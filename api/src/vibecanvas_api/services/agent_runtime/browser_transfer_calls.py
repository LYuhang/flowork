"""Durable file-transfer approval, scoped to one live Browser CLI command.

Selection is checked independently of approval mode. The returned transfer ID
is only a reference: each actual native file read revalidates its DB decision,
command lease, current identity, exact candidate and browser fence.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from vibecanvas_api.config import config
from vibecanvas_api.services.agent_resources.capability import verify_agent_capability
from vibecanvas_api.services.agent_resources.context import resolve_context
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.hitl_repo import HitlRepo
from .browser_download_choices import BrowserTransportUnavailable, SelectionRequired, current_scope, require_selection


class DownloadTransfer(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    choice_set_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)


class UploadFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1)
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern="^[a-f0-9]{64}$")


class UploadTransfer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    direction: Literal["upload"]
    tab_id: str = Field(pattern="^tab_.+$")
    destination: str = Field(min_length=1)
    files: list[UploadFile] = Field(min_length=1)


def _input(value):
    return UploadTransfer.model_validate(value) if isinstance(value, dict) and value.get("direction") == "upload" else DownloadTransfer.model_validate(value)


def _owned(row, cap):
    resume = row.resume_payload_json or {}
    if (row.chat_id != cap.chat_id or row.run_id != cap.turn_id
            or resume.get("user_id") != cap.user_id or resume.get("runtime_session_id") != cap.runtime_session_id
            or (row.runtime_correlation_json or {}).get("source") != "browser_transfer"):
        raise PermissionError("The file transfer belongs to a different Agent command.")


async def _preflight(session, cap, input):
    scope = await current_scope(session, cap)
    if isinstance(input, UploadTransfer):
        # Upload bytes and their hashes are frozen by the sandbox adapter before
        # prompting. The scoped CDP connection separately fences the target;
        # the adapter rechecks the exact document before sending any bytes.
        return scope, {"tab_id": input.tab_id, "capture_id": None}, None
    row, candidate = await require_selection(session, cap, input.choice_set_id, input.candidate_id)
    return scope, row, candidate


async def dispatch(token: str, arguments: dict) -> dict:
    cap = verify_agent_capability(token, secret=config.signing_secret, server="browser")
    if cap is None:
        raise PermissionError("Invalid or expired Browser CLI identity.")
    action = arguments.get("action")
    call_id = str(arguments.get("call_id") or "")
    if not call_id or len(call_id) > 128:
        raise ValueError("A bounded file-transfer call ID is required.")
    key = uuid.uuid5(uuid.NAMESPACE_URL, f"browser-transfer:{cap.tenant_id}:{cap.turn_id}:{cap.runtime_session_id}:{call_id}").hex
    transfer_id = "hitl_" + key
    params = {"id": transfer_id, "run": cap.turn_id, "tenant": cap.tenant_id}
    if action != "finish":
        await resolve_context(cap)
    async with session_scope(tenant_id=cap.tenant_id) as session:
        repo = HitlRepo(session)
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 0))"), params)
        row = await repo.get_request(transfer_id)
        events = []
        if action == "start":
            input = _input(arguments.get("input"))
            if cap.approval_mode not in {"always_allow", "always_ask", "agent"}:
                raise PermissionError("Unknown approval mode. No file bytes were read.")
            try:
                scope, capture, candidate = await _preflight(session, cap, input)
            except SelectionRequired as error:
                return {"status": "selection_required", "message": str(error), "_events": []}
            if row is None:
                from .orchestrator import AgentRuntimeOrchestrator
                from .protocol import RuntimeEvent

                await session.execute(text("""INSERT INTO interactive_call_leases(call_id,tenant_id,run_id,expires_at)
                    VALUES (:id,CAST(:tenant AS uuid),:run,now()+interval '60 seconds')"""), params)
                upload = isinstance(input, UploadTransfer)
                prompt = (f"Upload {len(input.files)} file(s) ({sum(file.bytes for file in input.files)} bytes) "
                          f"from this Chat's cloud sandbox to {input.destination}? "
                          + "; ".join(f"{file.name!r} ({file.bytes} bytes)" for file in input.files)
                          + ". The page may send them to its server.") if upload else (
                          f"Transfer {candidate.name!r} ({candidate.bytes} bytes, {candidate.source}) "
                          "from your computer to this Chat's cloud sandbox? The local file will be kept.")
                tool = "flowork-cli browser upload" if upload else "flowork-cli browser download"
                prepared = AgentRuntimeOrchestrator._prepare_approval(RuntimeEvent(
                    event_id=uuid.uuid4().hex, runtime_type="codex", runtime_session_id=cap.runtime_session_id,
                    chat_id=cap.chat_id, turn_id=cap.turn_id, seq=1, type="approval.required", payload={
                        "hitl_request_id": transfer_id, "title": "Upload files to browser" if upload else "Transfer downloaded file", "prompt_text": prompt,
                        "agent_payload": {"tool": tool, "arguments": input.model_dump() if upload else candidate.model_dump(exclude={"status"})},
                        "runtime_correlation": {"source": "browser_transfer", "runtime_request_id": transfer_id,
                            "runtime_method": "browser.upload" if upload else "browser.download", "runtime_item_id": "transfer_" + key}}))
                payload = dict(prepared.payload)
                private = payload.pop("_persist")
                auto = cap.approval_mode == "always_allow"
                artifact_id = None if auto else private["artifact_id"]
                if artifact_id:
                    await repo.create_interactive_artifact(artifact_id=artifact_id, tenant_id=cap.tenant_id,
                        chat_id=cap.chat_id, run_id=cap.turn_id, component_type="approval", completion_mode="wait_for_submit",
                        title=payload["title"], definition_json=private["definition"], artifact_ref=None, content_hash=None)
                row = await repo.create_request(hitl_request_id=transfer_id, tenant_id=cap.tenant_id, chat_id=cap.chat_id,
                    run_id=cap.turn_id, artifact_id=artifact_id, hitl_type="pre_tool_approval", title=payload["title"],
                    prompt_text=prompt, ui_payload_json=payload if not auto else {}, agent_payload_json=private["agent_payload"],
                    runtime_correlation_json=private["runtime_correlation"], mark_run_waiting=False,
                    resume_payload_json={"input": input.model_dump(), "scope": asdict(scope), "capture_id": capture["capture_id"],
                        "tab_id": capture["tab_id"], "user_id": cap.user_id, "runtime_session_id": cap.runtime_session_id,
                        "automatic": auto})
                if artifact_id:
                    await repo.link_artifact_hitl(artifact_id, transfer_id)
                if auto:
                    row, _ = await repo.resolve(hitl_request_id=transfer_id, decision="approve",
                                               decision_payload={"approval_mode": "always_allow", "automatic": True})
                else:
                    events = [{"event_type": "HITL_REQUIRED", "payload": payload},
                        {"event_type": "CHAT_EVENT", "payload": {"type": "tool_start", "tool_call_id": "transfer_" + key,
                            "message_id": "transfer_message_" + key,
                            "name": tool, "arguments": json.dumps(private["agent_payload"]["arguments"])}},
                        {"event_type": "CHAT_EVENT", "payload": payload["projection_event"]}]
            else:
                _owned(row, cap)
                if row.resume_payload_json.get("input") != input.model_dump():
                    raise ValueError("A transfer cannot change its file after approval starts.")
        elif action in {"poll", "renew", "finish"}:
            if row is None:
                return {"status": "expired", "message": "The file-transfer command is no longer active.", "_events": []}
            _owned(row, cap)
            if action == "finish":
                if row.status == "pending":
                    row, _ = await repo.resolve(hitl_request_id=transfer_id, decision="cancel",
                        decision_payload={"reason": "The file-transfer command ended."})
                await session.execute(text("DELETE FROM interactive_call_leases WHERE call_id=:id"), params)
                return {"status": "finished", "_events": [{"event_type": "HITL_RESOLVED", "payload": {
                    "hitl_request_id": transfer_id, "status": row.status}}]}
        else:
            raise ValueError("Unknown file-transfer lifecycle action.")
        # The human can wait indefinitely while the command remains live. A
        # transport reconnect pauses renewal, not the user's decision record.
        try:
            if action == "renew" and row.status == "approved":
                # A completed download may already have released its capture.
                # Every read still checks the capture; lease renewal checks the
                # live command and browser fence, not a finished file observer.
                scope = await current_scope(session, cap)
            else:
                scope, _, _ = await _preflight(session, cap, _input(row.resume_payload_json["input"]))
            if asdict(scope) != row.resume_payload_json["scope"]:
                raise PermissionError("Browser ownership changed during file-transfer approval.")
        except BrowserTransportUnavailable:
            return {"status": "reconnecting", "pending": True, "transfer_id": transfer_id, "_events": events}
        live = (await session.execute(text("""UPDATE interactive_call_leases SET expires_at=now()+interval '60 seconds'
            WHERE call_id=:id AND run_id=:run AND expires_at>now() RETURNING call_id"""), params)).first()
        if not live:
            return {"status": "expired", "message": "The file-transfer command is no longer active.", "_events": events}
        if row.status == "pending":
            return {"status": "awaiting_approval", "pending": True, "transfer_id": transfer_id, "_events": events}
        approved = row.status == "approved"
        return {"status": "approved" if approved else "denied" if row.status == "denied" else "cancelled",
            "transfer_id": transfer_id, "automatic": row.resume_payload_json.get("automatic", False),
            "message": ("File transfer allowed by approval mode: always_allow." if row.resume_payload_json.get("automatic")
                        else "The user approved this file transfer.") if approved else (
                            "No file bytes were sent to the browser. File transfer was not approved."
                            if row.resume_payload_json["input"].get("direction") == "upload"
                            else "No file bytes were read. File transfer was not approved."),
            "_events": events + ([] if action == "renew" else [{"event_type": "HITL_RESOLVED", "payload": {"hitl_request_id": transfer_id, "status": row.status}}])}


async def authorize_native_read(session, cap, *, transfer_id: str, capture_id: str) -> dict:
    """Called by trusted CDP host for EVERY chunk, before the extension reads."""
    await resolve_context(cap)
    row = await HitlRepo(session).get_request(transfer_id)
    if row is None:
        raise PermissionError("File transfer approval is required. No bytes were read.")
    _owned(row, cap)
    if row.status != "approved" or row.resume_payload_json.get("capture_id") != capture_id:
        raise PermissionError("The requested download was not approved. No bytes were read.")
    live = (await session.execute(text("""SELECT 1 FROM interactive_call_leases l JOIN agent_runs r ON r.run_id=l.run_id
        WHERE l.call_id=:id AND l.run_id=:run AND l.expires_at>now() AND r.status='running' AND r.cancel_requested_at IS NULL"""),
        {"id": transfer_id, "run": cap.turn_id})).first()
    if not live:
        raise PermissionError("The file-transfer command ended. No bytes were read.")
    scope, capture, candidate = await _preflight(session, cap, DownloadTransfer.model_validate(row.resume_payload_json["input"]))
    if asdict(scope) != row.resume_payload_json["scope"] or capture["tab_id"] != row.resume_payload_json["tab_id"]:
        raise PermissionError("Browser ownership changed. No bytes were read.")
    return {"tab_id": capture["tab_id"], "capture_id": capture_id, "candidate_id": candidate.candidate_id}
