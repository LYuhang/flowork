"""Host-owned SubAgent dependency resolution.

Workflow JSON contains only installation id/name. Published Skill revisions
are resolved once per execution; immutable paths are an internal run detail.
Callers supply a trusted, authorized execution principal and tenant session.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from uuid import UUID, uuid4

from vibecanvas_api.authorization.types import Action, PrincipalType, ResourceRef, ResourceType
from vibecanvas_api.storage.repo_mcp_servers import McpServersRepo
from vibecanvas_api.storage.repo_skills import SkillsRepo


def collect_subagent_resources(workflow: dict) -> dict[str, dict[str, list[dict]]]:
    result = {}
    for node_id, node in workflow.items():
        if node_id == "__meta__" or not isinstance(node, dict) or node.get("node_type") != "SubAgentNode":
            continue
        config = node.get("node_config") or {}
        selected = {}
        for collection in ("skills", "mcp_servers"):
            refs = config.get(collection, [])
            if not isinstance(refs, list):
                raise ValueError(f"{node_id}.{collection} must be an array")
            seen = set()
            selected[collection] = []
            for ref in refs:
                if (not isinstance(ref, dict) or set(ref) != {"id", "name"}
                        or not isinstance(ref.get("id"), str)
                        or not isinstance(ref.get("name"), str) or not ref["name"].strip()):
                    raise ValueError(f"{node_id}.{collection} requires id/name references only")
                identifier = str(UUID(ref["id"]))
                if identifier in seen:
                    raise ValueError(f"{node_id}.{collection} contains a duplicate installation")
                seen.add(identifier)
                selected[collection].append({"id": identifier, "name": ref["name"]})
        if any(selected.values()):
            result[node_id] = selected
    return result


async def resolve_workflow_resources(*, session, workflow, service, principal, context) -> dict:
    """Resolve the entire graph once, including the authorized soft Skill view.

    No credentials or connection configurations leave this function. An MCP
    broker must reauthorize each actual call; this snapshot is not permission.
    """
    nodes = collect_subagent_resources(workflow)
    if not nodes:
        return {"nodes": {}, "skills": [], "mcp_servers": []}
    organization = context.active_organization_id
    delegated = None
    if getattr(principal, "type", None) == PrincipalType.SERVICE_ACCOUNT:
        from vibecanvas_api.storage.repo_service_accounts import ServiceAccountsRepo
        delegated = set(await ServiceAccountsRepo(session).resource_refs(UUID(principal.id)))

    async def require(kind, identifier):
        if delegated is not None and (str(kind), identifier) not in delegated:
            raise PermissionError(f"workflow_resource_delegation_revoked:{kind}:{identifier}")
        decision = await service.check(principal, Action.USE,
            ResourceRef(kind, identifier, organization), context)
        if not decision.allowed:
            raise PermissionError(f"workflow_resource_unavailable:{kind}:{identifier}")

    skill_repo = SkillsRepo(session)
    selected_skills = {ref["id"] for node in nodes.values() for ref in node["skills"]}
    skill_rows = []
    if selected_skills:
        authorized = await service.list_authorized_ids(principal, Action.USE, ResourceType.SKILL_INSTALLATION, context)
        skill_rows = await skill_repo.list_authorized(authorized)
    resolved_skills = {}
    for row in skill_rows:
        identifier = str(row["skill_id"])
        if delegated is not None and ("skill_installation", identifier) not in delegated:
            continue
        revision_hash = str(row.get("revision_hash") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", revision_hash):
            continue
        # Installation HEAD and immutable revision id are read together by get/list.
        revision_id = str(row["current_revision_id"])
        await require(ResourceType.SKILL_INSTALLATION, identifier)
        revision_allowed = await service.check(principal, Action.USE,
            ResourceRef(ResourceType.SKILL_REVISION, revision_id, organization), context)
        if not revision_allowed.allowed:
            if identifier in selected_skills:
                raise PermissionError(f"workflow_skill_revision_unavailable:{identifier}")
            continue
        resolved_skills[identifier] = {
            "id": identifier, "name": row["name"], "description": row.get("description") or "",
            "revision_id": revision_id, "revision_hash": revision_hash,
            "root_path": f"/skills/{identifier}/{revision_hash}",
        }
    if selected_skills - resolved_skills.keys():
        raise PermissionError("workflow_skill_unavailable_or_unpublished")

    resolved_mcp = {}
    for identifier in sorted({ref["id"] for node in nodes.values() for ref in node["mcp_servers"]}):
        await require(ResourceType.MCP_INSTALLATION, identifier)
        row = await McpServersRepo(session).get(UUID(identifier))
        if (row is None or not row.get("enabled")
                or row.get("connection_status") not in {"connected", "not_required"}):
            raise PermissionError(f"workflow_mcp_unavailable:{identifier}")
        tools = row.get("last_tool_names")
        if not isinstance(tools, list) or any(not isinstance(tool, dict) or not tool.get("name") for tool in tools):
            raise ValueError(f"workflow_mcp_definitions_unavailable:{identifier}")
        definitions = [{key: tool[key] for key in ("name", "description", "input_schema", "output_schema") if key in tool}
                       for tool in tools]
        from jsonschema import Draft202012Validator, SchemaError
        for definition in definitions:
            try:
                Draft202012Validator.check_schema(definition.get("input_schema") or {"type": "object"})
                if definition.get("output_schema") is not None:
                    Draft202012Validator.check_schema(definition["output_schema"])
            except SchemaError as exc:
                raise ValueError(f"workflow_mcp_invalid_tool_schema:{identifier}") from exc
        if len({tool["name"] for tool in definitions}) != len(definitions):
            raise ValueError(f"workflow_mcp_duplicate_tool:{identifier}")
        resolved_mcp[identifier] = {"id": identifier, "name": row["name"], "tools": definitions,
            "tools_fingerprint": hashlib.sha256(json.dumps(definitions, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    for selected in nodes.values():
        selected["skills"] = [dict(resolved_skills[ref["id"]]) for ref in selected["skills"]]
        selected["mcp_servers"] = [dict(resolved_mcp[ref["id"]]) for ref in selected["mcp_servers"]]
    return {"nodes": nodes, "skills": list(resolved_skills.values()), "mcp_servers": list(resolved_mcp.values())}


def publish_skill_files(root: str, *, skill_id: str, revision_hash: str, files: list) -> str:
    """Add an immutable version without replacing an existing mount root.

    root is a host-owned identity-scoped directory, never a caller-supplied path.
    Bind it read-only into the sandbox. Concurrent publishers may reuse only a
    fully published directory; staged partial packages are outside the mount.
    """
    identifier = str(UUID(skill_id))
    if not re.fullmatch(r"[a-f0-9]{64}", revision_hash):
        raise ValueError("Invalid Skill revision hash")
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = root_path / identifier
    parent.mkdir(exist_ok=True, mode=0o700)
    destination = parent / revision_hash
    if destination.is_dir():
        return str(destination)
    # Sibling of mount root: partial contents are never visible inside /skills.
    staging = Path(tempfile.mkdtemp(prefix=".skill-publish-", dir=root_path.parent))
    try:
        seen = set()
        for relative, _content_type, data in files:
            if (not isinstance(relative, str) or "\\" in relative or
                    any(part in {"", ".", ".."} for part in relative.split("/")) or
                    any(ord(char) < 32 for char in relative) or relative in seen):
                raise ValueError("Invalid Skill package path")
            seen.add(relative)
            target = staging.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        if "SKILL.md" not in seen:
            raise ValueError("Skill package is missing SKILL.md")
        try:
            os.rename(staging, destination)
        except OSError:
            if not destination.is_dir():
                raise
        return str(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


async def materialize_workflow_skills(*, session, root: str, snapshot: dict) -> None:
    repo = SkillsRepo(session)
    for descriptor in snapshot["skills"]:
        files = await repo.read_revision_files(UUID(descriptor["id"]), UUID(descriptor["revision_id"]))
        if files is None:
            raise ValueError("workflow_skill_snapshot_unavailable")
        await asyncio.to_thread(publish_skill_files, root, skill_id=descriptor["id"],
            revision_hash=descriptor["revision_hash"], files=files)


async def prepare_execution_resources(*, sandbox_session, workflow: dict, tenant_id: str,
        user_id: str, workflow_id: str, execution_id: str, execution_resource_type: str,
        principal_type: str = "user", principal_id: str | None = None,
        principal_generation: int = 0) -> dict:
    """Prepare once per run/batch, from host-owned workflow and execution IDs."""
    if not collect_subagent_resources(workflow):
        return {}
    from types import SimpleNamespace
    from starlette.requests import Request
    from vibecanvas_api.authorization.openfga_client import openfga_client_from_config
    from vibecanvas_api.config import config
    from vibecanvas_api.services.agent_runtime.model_capability import authorization_model_generation
    from vibecanvas_api.services.agent_runtime.workflow_mcp_capability import mint_workflow_mcp_capability
    from vibecanvas_api.services.workflow_execution_authorization import authorize_workflow_execution

    claims = dict(organization_id=tenant_id, user_id=user_id, workflow_id=workflow_id,
        execution_id=execution_id, execution_resource_type=execution_resource_type,
        principal_type=principal_type, principal_id=principal_id or user_id,
        principal_generation=principal_generation,
        authorization_generation=authorization_model_generation(model_id=config.openfga_authorization_model_id))
    client = openfga_client_from_config()
    request = Request({"type": "http", "method": "POST", "path": "/internal/workflow-resources",
        "headers": [], "query_string": b"", "app": SimpleNamespace(state=SimpleNamespace(openfga_client=client))})

    async def resolve(*, session, service, principal, authz_context, capability):
        return await resolve_workflow_resources(session=session, workflow=workflow,
            service=service, principal=principal, context=authz_context)

    try:
        snapshot = await authorize_workflow_execution(request, SimpleNamespace(**claims), resolve=resolve)
    finally:
        await client.close()
    snapshot["execution"] = claims
    snapshot["lease_id"] = uuid4().hex
    await sandbox_session.prepare_workflow_skills(snapshot)
    for server in snapshot["mcp_servers"]:
        server["broker_url"] = (config.mcp.platform_internal_base_url.rstrip("/")
                                + f"/api/internal/workflow-mcp/v1/{server['id']}")
        server["capability"] = mint_workflow_mcp_capability(**claims, server_id=server["id"],
            tools_fingerprint=server["tools_fingerprint"], secret=config.signing_secret,
            ttl_s=config.mcp.runtime_model_capability_ttl_s)
    servers = {server["id"]: server for server in snapshot["mcp_servers"]}
    for node in snapshot["nodes"].values():
        node["mcp_servers"] = [dict(servers[server["id"]]) for server in node["mcp_servers"]]
    return snapshot


async def prepare_ephemeral_resources(*, root: str, workflow: dict, claims: dict) -> dict:
    """Use the same execution authorization for an isolated one-shot mount."""
    from types import SimpleNamespace
    from vibecanvas_api.storage.db import short_session_scope

    async def materialize(snapshot):
        async with short_session_scope(tenant_id=claims["tenant_id"]) as session:
            await materialize_workflow_skills(session=session, root=root, snapshot=snapshot)

    return await prepare_execution_resources(
        sandbox_session=SimpleNamespace(prepare_workflow_skills=materialize),
        workflow=workflow, **claims,
    )


async def canonicalize_resource_names(*, session, workflow, service, principal, context):
    """Refresh display names on save without exposing inaccessible resources.

    Missing references remain editable; execution performs the strict USE check.
    """
    import copy
    nodes = collect_subagent_resources(workflow)
    result = copy.deepcopy(workflow)
    names = {}
    for node_id, selected in nodes.items():
        for collection, kind, repo_type in (
            ("skills", ResourceType.SKILL_INSTALLATION, SkillsRepo),
            ("mcp_servers", ResourceType.MCP_INSTALLATION, McpServersRepo),
        ):
            for ref in selected[collection]:
                key = (kind, ref["id"])
                if key not in names:
                    decision = await service.check(principal, Action.USE,
                        ResourceRef(kind, ref["id"], context.active_organization_id), context)
                    row = await repo_type(session).get(UUID(ref["id"])) if decision.allowed else None
                    names[key] = row["name"] if row else None
                if names[key] is not None:
                    ref["name"] = names[key]
            if collection in result[node_id]["node_config"]:
                result[node_id]["node_config"][collection] = selected[collection]
    return result
