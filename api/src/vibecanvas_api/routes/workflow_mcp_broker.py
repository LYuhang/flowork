"""Workflow MCP calls, authorized on Host and executed in an isolated worker."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from jsonschema import Draft202012Validator, ValidationError, SchemaError
import structlog

from vibecanvas_api.authorization.types import Action, PrincipalType, ResourceRef, ResourceType
from vibecanvas_api.config import config
from vibecanvas_api.routes.runtime_mcp_broker import _bounded_body, _extract_capability
from vibecanvas_api.services.agent_runtime.workflow_mcp_capability import verify_workflow_mcp_capability
from vibecanvas_api.services.mcp_config import server_descriptor, validate_mcp_connection_destination
from vibecanvas_api.services.mcp_connection_secrets import hydrate_connection_credentials
from vibecanvas_api.services.mcp_oauth import resolve_oauth_auth_config
from vibecanvas_api.services.sandbox.manager import get_sandbox_manager
from vibecanvas_api.services.workflow_execution_authorization import authorize_workflow_execution
from vibecanvas_api.storage.repo_mcp_servers import McpServersRepo

router = APIRouter(tags=["workflow-mcp-broker"])
logger = structlog.get_logger(__name__)


async def authorize_selected_server(*, session, service, principal, authz_context, capability):
    if getattr(principal, "type", None) == PrincipalType.SERVICE_ACCOUNT:
        from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
        delegated = await ServiceAccountsRepo(session).resource_refs(UUID(principal.id))
        if ("mcp_installation", capability.server_id) not in delegated:
            raise HTTPException(403, detail={"code": "workflow_mcp_access_revoked"})
    decision = await service.check(principal, Action.USE,
        ResourceRef(ResourceType.MCP_INSTALLATION, capability.server_id, capability.organization_id), authz_context)
    if not decision.allowed:
        raise HTTPException(403, detail={"code": "workflow_mcp_access_revoked"})
    row = await McpServersRepo(session).get(UUID(capability.server_id))
    if row is None or not row.get("enabled") or row.get("connection_status") not in {"connected", "not_required"}:
        raise HTTPException(403, detail={"code": "workflow_mcp_unavailable"})
    return row


async def resolve_call(*, session, service, principal, authz_context, capability, body):
    row = await authorize_selected_server(session=session, service=service, principal=principal,
        authz_context=authz_context, capability=capability)
    definitions = [{key: tool[key] for key in ("name", "description", "input_schema", "output_schema") if key in tool}
                   for tool in row.get("last_tool_names") or [] if isinstance(tool, dict)]
    fingerprint = hashlib.sha256(json.dumps(definitions, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if fingerprint != capability.tools_fingerprint:
        raise HTTPException(409, detail={"code": "workflow_mcp_definitions_changed"})
    tool = next((item for item in definitions if item.get("name") == body["tool_name"]), None)
    if tool is None:
        raise HTTPException(403, detail={"code": "workflow_mcp_tool_not_selected"})
    schema = tool.get("input_schema") or {"type": "object", "properties": {}}
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(body["arguments"])
    except (ValidationError, SchemaError) as exc:
        raise HTTPException(422, detail={"code": "workflow_mcp_invalid_arguments"}) from exc
    hydrated = await hydrate_connection_credentials(session, row)
    auth_config = await resolve_oauth_auth_config(session, hydrated)
    if auth_config is None:
        raise HTTPException(403, detail={"code": "workflow_mcp_authorization_required"})
    descriptor = server_descriptor({**hydrated, "auth_config": auth_config})
    allowed_hosts = await validate_mcp_connection_destination(descriptor["connection"])
    return {"action": "call", "connection": descriptor["connection"],
            "tool_name": body["tool_name"], "arguments": body["arguments"],
            "input_schema": schema, "timeout_s": 120.0}, sorted(allowed_hosts)


async def watch_call_authorization(request, capability, owner):
    """Stop queued/running work on disconnect, execution end, or revocation."""
    try:
        while True:
            await asyncio.sleep(1)
            if await request.is_disconnected():
                owner.cancel()
                return
            await authorize_workflow_execution(request, capability, resolve=authorize_selected_server)
    except asyncio.CancelledError:
        raise
    except Exception:
        owner.cancel()


@router.post("/api/internal/workflow-mcp/v1/{server_id}", include_in_schema=False)
async def workflow_mcp_call(request: Request, server_id: str):
    capability = verify_workflow_mcp_capability(_extract_capability(request),
        secret=config.signing_secret, server_id=server_id)
    if capability is None:
        raise HTTPException(401, detail={"code": "workflow_mcp_capability_invalid"})
    try:
        body = json.loads(await _bounded_body(request))
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(422, detail={"code": "workflow_mcp_invalid_request"}) from exc
    if (not isinstance(body, dict) or set(body) != {"tool_name", "arguments"}
            or not isinstance(body["tool_name"], str) or not body["tool_name"]
            or not isinstance(body["arguments"], dict)):
        raise HTTPException(422, detail={"code": "workflow_mcp_invalid_request"})

    async def resolve(**kwargs):
        return await resolve_call(**kwargs, body=body)

    started = time.monotonic()
    watcher = None
    try:
        operation, hosts = await authorize_workflow_execution(request, capability, resolve=resolve)
        watcher = asyncio.create_task(watch_call_authorization(request, capability, asyncio.current_task()))
        # This private one-shot sandbox never mounts the user's workflow files.
        # Its credential-bearing request is removed by provider teardown.
        result = await get_sandbox_manager().run_mcp_probe(capability.organization_id,
            operation, timeout=120.0, allow_hosts=hosts)
        if result.get("status") != "ok" or not isinstance(result.get("result"), dict):
            raise HTTPException(502, detail={"code": "workflow_mcp_call_failed", "outcome": "unknown"})
        logger.info("workflow_mcp_call_completed", execution_id=capability.execution_id,
            server_id=capability.server_id, tool_name=body["tool_name"],
            tools_fingerprint=capability.tools_fingerprint,
            duration_ms=round((time.monotonic() - started) * 1000),
            tool_error=bool(result["result"].get("isError")))
        return {"result": result["result"]}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("workflow_mcp_call_failed", execution_id=capability.execution_id,
            server_id=capability.server_id, error_type=type(exc).__name__,
            duration_ms=round((time.monotonic() - started) * 1000))
        raise HTTPException(502, detail={"code": "workflow_mcp_call_failed", "outcome": "unknown"}) from exc

    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
