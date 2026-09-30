"""Read-only resource discovery through the same policy as resource pages."""
from uuid import UUID

from fastapi import HTTPException

from vibecanvas_api.authorization.types import Action, ConsistencyPreference
from vibecanvas_api.flowork_cli.resource_cli import validate
from vibecanvas_api.routes import mcp_servers, skills
from vibecanvas_api.services.agent_runtime.resource_routes import resource_route_params
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_mcp_servers import McpServersRepo
from vibecanvas_api.storage.repo_skills import SkillsRepo


def metadata(row, resource):
    """Allowlist output: never export connection configuration or credentials."""
    row = row.model_dump() if hasattr(row, "model_dump") else row
    fields = ("name", "description", "source", "version", "revision_hash") if resource == "skill" else (
        "name", "description", "source", "transport", "enabled", "connection_status",
        "last_handshake_status", "last_handshake_at", "last_tool_count")
    identifier = "skill_id" if resource == "skill" else "server_id"
    return {"id": str(row.get(identifier) or row["id"]),
            **{key: row[key].isoformat() if hasattr(row[key], "isoformat") else row[key]
               for key in fields if key in row}}


async def read(context, operation, arguments):
    args = validate(operation, arguments)
    resource, action = operation.split(".")
    try:
        async with session_scope(tenant_id=context.tenant_id) as session:
            params = resource_route_params(context, session)
            auth = {key: val for key, val in params.items() if key != "session"}
            auth["consistency"] = ConsistencyPreference.HIGHER_CONSISTENCY
            if action == "list":
                response = await (skills.list_skills if resource == "skill" else mcp_servers.list_mcp_servers)(**params)
                rows = [row.model_dump() if hasattr(row, "model_dump") else row for row in response["items"]]
                search = args["search"].casefold()
                rows = [metadata(row, resource) for row in rows
                        if "use" in row.get("access", {}).get("capabilities", [])
                        and search in (row.get("name", "") + " " + (row.get("description") or "")).casefold()]
                rows.sort(key=lambda row: (row.get("name", "").casefold(), row["id"]))
                start, stop = args["offset"], args["offset"] + args["limit"]
                return {"status": "succeeded", "items": rows[start:stop], "total": len(rows),
                        "next_offset": stop if stop < len(rows) else None}
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
            files = await repo.read_revision_files(identifier, revision["revision_id"])
            if files is None:
                raise HTTPException(404, "skill_version_unavailable")
            result = {"status": "succeeded", **metadata(row, resource),
                      "revision_hash": revision_hash, "version": revision["version"],
                      "is_latest": bool(revision["is_latest"])}
            if action in {"get", "files"}:
                result["files"] = [{"path": path, "content_type": content_type, "size_bytes": len(data)}
                                   for path, content_type, data in sorted(files)]
                result["entrypoint"] = "SKILL.md"
                return result
            found = next((data for path, _content_type, data in files if path == args["path"]), None)
            if found is None:
                raise HTTPException(404, "skill_file_unavailable")
            try:
                content = found.decode("utf-8")
                if "\x00" in content:
                    raise UnicodeError("binary file")
            except UnicodeError:
                return {**result, "error": "binary_file", "status": "failed",
                        "message": "This file is not UTF-8 text. Read the Skill instructions for its intended use."}
            start, stop = args["offset"], args["offset"] + args["limit"]
            return {**result, "path": args["path"], "content": content[start:stop],
                    "total_characters": len(content), "next_offset": stop if stop < len(content) else None}
    except HTTPException as exc:
        return {"status": "failed", "error": "resource_unavailable", "message": str(exc.detail),
                "hint": "Discover an accessible installed resource with skill list or mcp list; check its published version and current use permission."}
