"""Read-only resource discovery through the same policy as resource pages."""
from uuid import UUID

from fastapi import HTTPException

from vibecanvas_api.authorization.types import Action, ConsistencyPreference
from vibecanvas_api.flowork_cli.resource_cli import validate
from vibecanvas_api.routes import mcp_servers, skills
from vibecanvas_api.services.agent_runtime.resource_routes import resource_route_params, admitted_resource_route_params, shared_resource_cards
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_mcp_servers import McpServersRepo
from vibecanvas_api.storage.repo_skills import SkillsRepo


def metadata(row, resource):
    """Allowlist output: never export connection configuration or credentials."""
    row = row.model_dump() if hasattr(row, "model_dump") else row
    fields = ("name", "description", "source", "version", "revision_hash", "installed") if resource == "skill" else (
        "name", "description", "source", "transport", "enabled", "connection_status",
        "last_handshake_status", "last_handshake_at", "last_tool_count")
    identifier = "skill_id" if resource == "skill" else "server_id"
    return {"id": str(row.get(identifier) or row["id"]),
            **{key: row[key].isoformat() if hasattr(row[key], "isoformat") else row[key]
               for key in fields if key in row}}


RUNTIME_HINT = "Sharing does not install a Skill. Check installed; use skill install --skill-id ID to opt in, then skill refresh --skill-id ID for this turn. runtime_path is available only when installed; inspect it with bash ls/find/cat."


def runtime_location(context, identifier):
    from vibecanvas_api.services.runtime_skills import runtime_skill_root
    if not context.chat_id:
        return {}
    path = runtime_skill_root(context.chat_id, str(identifier))
    return {"runtime_path": path, "entrypoint": f"{path}/SKILL.md"}


async def read(context, operation, arguments):
    args = validate(operation, arguments)
    resource, action = operation.split(".")
    try:
        async with session_scope(tenant_id=context.tenant_id, user_id=context.username) as session:
            params = resource_route_params(context, session)
            auth = {key: val for key, val in params.items() if key != "session"}
            auth["consistency"] = ConsistencyPreference.HIGHER_CONSISTENCY
            if action == "list":
                response = await (skills.list_skills if resource == "skill" else mcp_servers.list_mcp_servers)(**params)
                rows = [row.model_dump() if hasattr(row, "model_dump") else row for row in response["items"]]
                rows = [metadata(row, resource) for row in rows
                        if "use" in row.get("access", {}).get("capabilities", [])]
                if resource == "skill":
                    seen = {row["id"] for row in rows}
                    for card in await shared_resource_cards(context, session, "skill_installation"):
                        if card.resource_id not in seen and "use" in card.access.capabilities:
                            rows.append({"id": card.resource_id, "name": card.name,
                                         "description": card.description, "source": "custom"})
                            seen.add(card.resource_id)
                    from vibecanvas_api.storage.repo_skill_installations import SkillInstallationsRepo
                    installed = await SkillInstallationsRepo(session).installed_ids(UUID(context.username))
                    rows = [{**row, "installed": row["id"] in installed,
                             **(runtime_location(context, row["id"]) if row["id"] in installed else {})} for row in rows]
                rows.sort(key=lambda row: (row.get("name", "").casefold(), row["id"]))
                start, stop = args["offset"], args["offset"] + args["limit"]
                return {"status": "succeeded", "items": rows[start:stop], "total": len(rows),
                        "next_offset": stop if stop < len(rows) else None,
                        **({"runtime_hint": RUNTIME_HINT} if resource == "skill" else {})}
            if resource == "mcp":
                identifier = UUID(args["server_id"])
                await mcp_servers._authorize_mcp(server_id=identifier, action=Action.USE, **auth)
                row = await McpServersRepo(session).get(identifier)
                if row is None:
                    raise HTTPException(404, "resource_unavailable")
                result = metadata(row, resource)
                # Stored discovery only: this endpoint never starts a server or calls a tool.
                definitions = row.get("last_tool_names") or []
                result["tools"] = [{key: tool[key] for key in ("name", "description", "input_schema", "output_schema") if key in tool}
                                   for tool in definitions if isinstance(tool, dict)]
                result["definitions_source"] = "last_handshake"
                return {"status": "succeeded", **result}
            identifier = UUID(args["skill_id"])
            params = await admitted_resource_route_params(context, session, "skill_installation", identifier)
            auth = {key: val for key, val in params.items() if key != "session"}
            auth["consistency"] = ConsistencyPreference.HIGHER_CONSISTENCY
            await skills._authorize_skill(skill_id=identifier, action=Action.USE, **auth)
            repo = SkillsRepo(session)
            row = await repo.get(identifier)
            if row is None:
                raise HTTPException(404, "resource_unavailable")
            revision_hash = row.get("revision_hash")
            revisions = await repo.list_revisions(identifier)
            revision = next((item for item in revisions if item["revision_hash"] == revision_hash), None)
            if revision is None:
                raise HTTPException(404, "skill_version_unavailable")
            await skills._authorize_skill_revision(revision_id=revision["revision_id"], action=Action.USE, **auth)
            return {"status": "succeeded", **metadata(row, resource),
                    "revision_hash": revision_hash, "version": revision["version"],
                    "is_latest": bool(revision["is_latest"]),
                    **(runtime_location(context, identifier) if row.get("installed") else {}), "runtime_hint": RUNTIME_HINT}
    except HTTPException as exc:
        return {"status": "failed", "error": "resource_unavailable", "message": str(exc.detail),
                "hint": "Discover an accessible installed resource with skill list or mcp list; check its published version and current use permission."}
