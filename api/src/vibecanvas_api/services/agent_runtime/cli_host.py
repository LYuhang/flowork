"""Host-only CLI dispatch. Resource discovery is authorized here; the CLI holds no user credentials."""

from __future__ import annotations

from vibecanvas_api.services.write_conflicts import WriteConflict

from copy import deepcopy

import structlog
from vibecanvas_engine.nodes.base import BaseNode

from vibecanvas_api.agents.prompts.node_definitions import available_node_types, build_node_spec
from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.authorization.openfga_client import OpenFgaUnavailableError
from vibecanvas_api.config import config
from vibecanvas_api.flowork_cli.cli import OPERATIONS, WRITE_OPERATIONS, error, uncertain_result, validate_arguments
from vibecanvas_api.services.agent_resources import context as agent_context
from vibecanvas_api.services.agent_resources.authorization import (
    create_authorized_workflow,
    get_authorized_workflow_metadata,
    list_authorized_workflows,
    require_organization_create,
)
from vibecanvas_api.services.agent_resources.capability import verify_agent_capability
from vibecanvas_api.services.agent_resources.workflow_graph import (
    _node_count,
    collect_workflow_warnings,
    validate_workflow_for_context,
)
from vibecanvas_api.services.agent_resources.workflow_layout import layout_workflow
from vibecanvas_api.services.agent_resources.workflow_operations import operate_workflow
from vibecanvas_api.services.agent_resources.workflow_transfer import (
    download_workflow,
    read_workflow_snapshot,
    upload_workflow,
)
from vibecanvas_api.services.agent_resources.workflow_versions import workflow_version_command

logger = structlog.get_logger(__name__)


def _log_authorization_unavailable(operation: str, exc: OpenFgaUnavailableError) -> None:
    # Do not format exceptions or request objects: they may hold capabilities.
    logger.warning(
        "flowork_cli_authorization_unavailable", operation=operation,
        reason_code=exc.reason_code,
        transport_error=type(exc.__cause__).__name__ if exc.__cause__ else None,
    )


def _metadata_result(meta: dict) -> dict:
    return {
        "id": str(meta["wf_id"]), "name": meta.get("workflow_name") or "",
        "description": meta.get("description") or "", "tags": meta.get("tags") or [],
        "version": f"v{meta['active_major']}.sv{meta['active_sub']}",
    }


async def invoke_workflow_command(*, operation: str, identity_token: str, arguments: dict) -> dict:
    if operation == "workflow.list":
        return await invoke_workflow_list(identity_token=identity_token, arguments=arguments)
    if operation not in OPERATIONS:
        raise PermissionError("unsupported CLI operation")
    capability = verify_agent_capability(identity_token, secret=config.signing_secret, server="cli")
    if capability is None:
        raise PermissionError("invalid or expired CLI identity")
    try:
        arguments = validate_arguments(operation, arguments)
    except ValueError as exc:
        return error("invalid_arguments", str(exc), f"Run flowork-cli {operation.replace('.', ' ')} --help.")
    write_started = False
    try:
        if operation in {"cli.start", "cli.poll", "cli.cancel"}:
            from .cli_calls import command
            return await command(capability, operation, arguments, identity_token)
        if operation == "workflow.delete":
            return error("approval_required", "Deletion requires the live CLI approval channel.", "Use flowork-cli workflow delete --workflow-id ID.")
        if operation.startswith("browser."):
            return {"status": "failed", **error("browser_runtime_unavailable", "Browser CLI execution requires the sandbox browser runtime.", "Use flowork-cli browser from an active browser side-panel turn. Do not infer action success from this response.")}
        if operation.startswith(("task.", "deployment.", "knowledge.")) or operation in {"skill.create", "skill.update", "skill.check", "skill.download", "skill.refresh", "skill.delete", "skill.install", "skill.uninstall"}:
            return error("live_channel_required", "This command requires the live CLI channel.", "Use flowork-cli --help.")
        context = await agent_context.resolve_context(capability)
        if operation == "workflow.prepare":
            from .cli_local_prepare import prepare
            return await prepare(context, arguments)
        if operation.startswith(("skill.", "mcp.")):
            from .cli_resources import read
            return await read(context, operation, arguments)
        if operation == "config.get":
            from .cli_config import get_config
            return await get_config(context, arguments)
        if operation == "workflow.operation":
            write_started = True
            return await operate_workflow(context, arguments)
        if operation == "workflow.layout":
            write_started = True
            return await layout_workflow(context, **arguments)
        if operation == "workflow.get-spec":
            # Live turn identity is required, but resource binding/graph access
            # is not: these are platform definitions, not workflow content.
            candidates = available_node_types()
            if arguments.get("list_types"):
                return {"types": candidates}
            unknown = [name for name in arguments["node_types"] if name not in candidates]
            if unknown:
                return {**error("unknown_node_type", "Unknown node type(s): " + ", ".join(unknown) + ".",
                               "Use exact case-sensitive names from flowork-cli workflow get-spec --list-types."),
                        "types": candidates}
            return {"node_schema": deepcopy(BaseNode.GENERAL_NODE_SCHEMA),
                    "specs": [build_node_spec(name) for name in arguments["node_types"]]}
        if operation.startswith("workflow.version."):
            write_started = operation in WRITE_OPERATIONS
            result = await workflow_version_command(context, operation, arguments)
            logger.info("flowork_cli_completed", operation=operation, chat_id=capability.chat_id,
                        turn_id=capability.turn_id, user_id=capability.user_id,
                        workflow_id=result["id"], version=result["version"])
            return result
        if operation == "workflow.check":
            reference = {}
            if "workflow" in arguments:
                # Never load a saved graph or infer authority from file metadata.
                workflow = deepcopy(arguments["workflow"])
            else:
                snapshot = await read_workflow_snapshot(context, workflow_id=arguments['workflow_id'], major=arguments['major'])
                workflow = snapshot["workflow"]
                reference = {"id": snapshot["id"], "version": snapshot["version"]}
            errors = await validate_workflow_for_context(workflow, context)
            warnings = collect_workflow_warnings(workflow)
            result = {"valid": not errors, **reference, "node_count": _node_count(workflow)}
            if errors:
                result["errors"] = [{"node_id": item.get("node_id", "global"), "message": item["message"]} for item in errors]
            if warnings:
                result["warnings"] = [{"node_id": item.get("node_id", "global"), "message": item["message"]} for item in warnings]
            logger.info("flowork_cli_completed", operation=operation, chat_id=capability.chat_id,
                        turn_id=capability.turn_id, user_id=capability.user_id, valid=result["valid"])
            return result
        if operation in {"workflow.download", "workflow.upload"}:
            if operation == "workflow.upload":
                write_started = True
                result = await upload_workflow(context, arguments["workflow"], workflow_id=arguments["workflow_id"], major=arguments["major"], expected_version=arguments["expected_version"], note=arguments["note"])
            else:
                result = await download_workflow(context, workflow_id=arguments['workflow_id'], major=arguments['major'])
            logger.info("flowork_cli_completed", operation=operation, chat_id=capability.chat_id,
                        turn_id=capability.turn_id, user_id=capability.user_id, workflow_id=result["id"],
                        version=result["version"])
            return result
        if operation in {"workflow.get", "workflow.update"}:
            write_started = operation == "workflow.update"
            changes = {k: v for k, v in arguments.items() if k != "workflow_id"} if write_started else None
            meta = await get_authorized_workflow_metadata(context, arguments["workflow_id"], changes)
        else:
            await require_organization_create(context)
            workflow = deepcopy(arguments.get("workflow"))
            if workflow is not None:
                if not isinstance(workflow.get("__meta__", {}), dict):
                    return error("invalid_workflow", "__meta__ must be an object.", "Repair the workflow file before creating.")
                # Old resource identity/metadata is not imported. Preserve only
                # operational metadata; storage stamps the new ID and version.
                metadata = workflow.setdefault("__meta__", {})
                for key in ("workflow_id", "workflow_name", "workflow_version", "workflow_subversion", "description", "tags", "owner_id", "tenant_id", "creator_user_id"):
                    metadata.pop(key, None)
                try:
                    errors = await validate_workflow_for_context(workflow, context)
                except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                    return error("invalid_workflow", "Workflow structure or node configuration is malformed.", "Repair the workflow JSON before creating.")
                if errors:
                    message = "; ".join(f"{item.get('node_id', 'global')}: {item.get('message', 'Invalid workflow')}" for item in errors[:10])
                    return error("invalid_workflow", message[:8000], "Repair the workflow file before creating. No workflow was created.")
            write_started = True
            snapshot = await create_authorized_workflow(
                context, name=arguments["name"], description=arguments["description"],
                tags=arguments["tags"], initial_workflow=workflow,
            )
            meta = snapshot.meta
        result = _metadata_result(meta)
        logger.info("flowork_cli_completed", operation=operation, chat_id=capability.chat_id,
                    turn_id=capability.turn_id, user_id=capability.user_id, workflow_id=result["id"])
        return result
    except WriteConflict as exc:
        return exc.cli_result()
    except OpenFgaUnavailableError as exc:
        _log_authorization_unavailable(operation, exc)
        return error("authorization_unavailable", "Authorization is temporarily unavailable.", "Retry when authorization is available.")
    except PermissionError as exc:
        if isinstance(exc.__cause__, OpenFgaUnavailableError):
            _log_authorization_unavailable(operation, exc.__cause__)
            return error("authorization_unavailable", "Authorization is temporarily unavailable.", "Retry when authorization is available.")
        return error("permission_denied", "The identity or Agent execution is no longer authorized.", "Check account access and start a new Agent turn.")
    except ToolError as exc:
        result = error(str(exc), exc.message or str(exc), "Check permissions and input before retrying.")
        if str(exc) == "workflow_unavailable":
            result["hint"] = "Use workflow list to find an accessible workflow and pass its exact ID."
        elif str(exc) == "version_not_found":
            result["hint"] = "Run workflow version list <id> and choose an existing --major."
        if isinstance(exc.info, dict) and exc.info.get("created"):
            result.update(exc.info)
            result["hint"] = "Do not create another copy. Use the returned ID after authorization recovers; contact support if access remains unavailable."
        return result
    except Exception:
        logger.exception("flowork_cli_failed", operation=operation, chat_id=capability.chat_id, turn_id=capability.turn_id)
        if write_started:
            return uncertain_result()
        return error("platform_unavailable", "The platform query could not be completed.", "Retry or contact support.")


async def invoke_workflow_list(*, identity_token: str, arguments: dict) -> dict:
    """Use Host-held base identity, then authorize workflow metadata separately.

    The private CLI capability supplies a live identity, NOT workflow
    permission. This explicit CLI allowlist is independent of slash activation;
    list_authorized_workflows enforces current resource permissions on every call.
    """
    capability = verify_agent_capability(identity_token, secret=config.signing_secret, server="cli")
    if capability is None:
        raise PermissionError("invalid or expired CLI identity")
    try:
        if set(arguments) - {"limit", "offset"}:
            raise ValueError("Unexpected workflow list parameter.")
        limit, offset = arguments.get("limit", 20), arguments.get("offset", 0)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("--limit must be between 1 and 100.")
        if type(offset) is not int or not 0 <= offset <= 2_147_483_647:
            raise ValueError("--offset must be between 0 and 2147483647.")
        context = await agent_context.resolve_context(capability)
        rows = await list_authorized_workflows(context, limit=limit + 1, offset=offset, include_access=False)
        result = {
            "workflows": [
                {
                    "id": str(row["wf_id"]),
                    "name": row.get("workflow_name") or "",
                    "description": row.get("description") or "",
                    "version": f"v{row['active_major']}.sv{row['active_sub']}",
                }
                for row in rows[:limit]
            ],
            "next_offset": offset + limit if len(rows) > limit else None,
        }
        logger.info("flowork_cli_completed", operation="workflow.list", chat_id=capability.chat_id,
                    turn_id=capability.turn_id, user_id=capability.user_id, count=len(result["workflows"]))
        return result
    except ValueError as exc:
        return error("invalid_arguments", str(exc), "Run flowork-cli workflow list --help.")
    except PermissionError:
        return error("permission_denied", "The identity or Agent execution is no longer authorized.", "Check account access and start a new Agent turn.")
    except ToolError as exc:
        return error(str(exc), exc.message or str(exc), "Check resource permissions or retry when authorization is available.")
    except Exception:
        logger.exception("flowork_cli_failed", operation="workflow.list", chat_id=capability.chat_id, turn_id=capability.turn_id)
        return error("platform_unavailable", "Workflow listing could not be completed.", "Retry this read-only query or contact support.")
