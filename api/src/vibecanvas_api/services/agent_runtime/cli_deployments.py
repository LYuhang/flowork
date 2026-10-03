"""Live Deployment CLI adapter over the authorized dashboard operations."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import uuid

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import text

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.auth.deps import require_recent_step_up
from vibecanvas_api.auth.repo import AuthRepo
from vibecanvas_api.authorization.types import Action
from vibecanvas_api.config import config
from vibecanvas_api.flowork_cli.cli import error, uncertain_result
from vibecanvas_api.flowork_cli.deployment_cli import WRITE_OPERATIONS
from vibecanvas_api.routes import deployments as routes
from vibecanvas_api.services.agent_resources import context as agent_context
from vibecanvas_api.services.agent_resources.authorization import _require_active_chat_write
from vibecanvas_api.services.agent_runtime.resource_routes import resource_route_params
from vibecanvas_api.services.deployment_snapshots import resolve_workflow
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_deployments import DeploymentsRepo
from vibecanvas_api.storage.workflow_history_repo import WorkflowHistoryRepo

_running_tests: set[asyncio.Task] = set()


def settings(arguments):
    result = {key: arguments[key] for key in ("name", "slug", "trigger_type", "enabled", "rate_limit_qps", "timeout_seconds") if key in arguments}
    if "workflow_id" in arguments:
        result["wf_id"] = arguments["workflow_id"]
    if "mount" in arguments:
        result["mount_enabled"] = arguments["mount"]
    if "major" in arguments:
        result.update(version_pin="major", pinned_major=int(arguments["major"][1:]), pinned_sub=None)
    if "version" in arguments:
        major, sub = arguments["version"][1:].split(".sv")
        result.update(version_pin="specific", pinned_major=int(major), pinned_sub=int(sub))
    return result


def deployment_status(dep):
    result = {"deployment_id": str(dep["id"]), "name": dep["name"], "workflow_id": dep["wf_id"],
        "trigger_type": dep["trigger_type"], "status": "enabled" if dep["enabled"] else "disabled",
        "slug": dep["slug"], "rate_limit_qps": dep["rate_limit_qps"], "mount": dep.get("mount_enabled", True), "timeout_seconds": dep.get("timeout_seconds", 30)}
    if dep["version_pin"] == "major":
        result["major"] = f"v{dep['pinned_major']}"
    elif dep["version_pin"] == "specific":
        result["version"] = f"v{dep['pinned_major']}.sv{dep['pinned_sub']}"
    else:
        result["version_policy"] = "legacy_head"
    path = f"/api/v1/deployments/{dep['slug']}/" + ("webhook" if dep["trigger_type"] == "webhook" else "invoke")
    result["endpoint"] = config.public_urls.absolute(path) if config.public_urls.public_url else None
    result["endpoint_path"] = path
    result["message"] = "The deployment is enabled. This does not confirm successful execution." if dep["enabled"] else "The deployment is disabled for new calls. In-flight side effects are not undone."
    result["desired_version"] = result.get("version")
    result["rollout_status"] = dep.get("rollout_status", "unknown")
    result["rollout_error"] = dep.get("rollout_error")
    runtime = dep.get("runtime")
    result["active_version"] = None
    if isinstance(runtime, dict):
        active = next((item for item in runtime.get("instances", [])
                       if item.get("state") == "active"
                       and str(item.get("id")) == str(dep.get("active_revision_id"))), None)
        result["active_version"] = active.get("version") if active else None
    result["active_version_known"] = isinstance(runtime, dict)
    if dep["enabled"]:
        result["message"] += " version/desired_version describe saved configuration, not completed rollout."
        if result["rollout_status"] != "ready":
            result["message"] += " Rollout is not ready; an existing active version may still serve new calls."
    result["hint"] = f"flowork-cli deployment info --deployment_id {dep['id']}"
    if result["endpoint"] is None:
        result["message"] += " The public URL is not configured; ask the operator to set VIBECANVAS_PUBLIC_URL."
    return result


def execution_status(row, deployment_id):
    result = {key: value for key, value in row.items() if key in {
        "status", "source", "trigger_type", "submitted_at", "started_at", "finished_at", "latency_ms", "result_summary"}}
    result.update(execution_id=str(row["id"]), deployment_id=str(deployment_id), execution_error=row.get("error"), execution_url=f"/workflow-executions/{row['id']}")
    result["message"] = "Invocation status: " + str(row["status"]) + ". Query success is not execution success."
    result["hint"] = f"flowork-cli deployment status --deployment_id {deployment_id} --execution_id {row['id']}"
    return result


def execution_result(summary, detail):
    """Project persisted EndNode outputs without exposing graph or node internals."""
    result = dict(summary)
    state = detail["status"] if detail else summary["status"]
    terminal = state in {"succeeded", "failed", "timed_out", "cancelled"}
    evidence = (detail or {}).get("result") or {}
    final = evidence.get("final_outputs")
    available = state == "succeeded" and isinstance(final, dict) and "__end__" in final
    reason = None if available else (
        "execution_pending" if not terminal else
        "execution_unsuccessful" if state != "succeeded" else "result_not_recorded")
    result.update(status=state, terminal=terminal, result_available=available,
                  outputs=final["__end__"] if available else None,
                  result_unavailable_reason=reason)
    if terminal and state != "succeeded":
        result["execution_error"] = (summary.get("execution_error")
            or (detail or {}).get("error_code") or state)
    result["message"] = ("Complete business outputs retrieved." if available else
        "Execution is still pending. Query this execution again; do not submit another call." if not terminal else
        "Execution did not succeed. Inspect execution_error and logs." if state != "succeeded" else
        "This execution has no retained complete result; the summary is not a substitute.")
    result["hint"] = (f"flowork-cli deployment result --deployment_id {result['deployment_id']} "
                      f"--execution_id {result['execution_id']}")
    return result


def execution_errors(errors):
    """Expose actionable node errors, not internal args/kwargs/runtime objects."""
    return {str(node): str(value.get("error_message") or value.get("message") or "Node execution failed.")
            if isinstance(value, dict) else str(value) for node, value in errors.items()}


def needs_approval(operation, args, current):
    if operation == "deployment.disable":
        return False
    if operation == "deployment.update":
        previous = current["rate_limit_qps"]
        rate = args.get("rate_limit_qps", previous)
        return args.get("timeout_seconds", current.get("timeout_seconds", 30)) > current.get("timeout_seconds", 30) or bool({"major", "version"} & args.keys()) or (args.get("mount") is True and not current.get("mount_enabled", True)) or (previous > 0 and (rate == 0 or rate > previous))
    return True


async def step_up(params):
    """Read current elevation from the server Session, never sandbox input."""
    auth = params["ctx"]
    if config.high_risk_step_up_required and auth.session_id:
        row = await AuthRepo(params["session"]).get_session_by_id(
            uuid.UUID(auth.session_id), user_id=uuid.UUID(auth.user_id))
        if (row is not None and row.expires_at > datetime.now(timezone.utc)
                and row.generation == auth.session_generation
                and str(row.active_organization_id) == auth.tenant_id):
            auth = replace(auth, authentication_strength=row.authentication_strength,
                step_up_expires_at=row.step_up_expires_at, session_audience=row.audience)
    params["ctx"] = await require_recent_step_up(auth)


def test_finished(task):
    _running_tests.discard(task)
    # Consume exceptions even when the observing CLI has disconnected. The
    # route records execution failures in history; never log inputs/secrets.
    if not task.cancelled():
        task.exception()


async def read(ctx, operation, args):
    async with session_scope(tenant_id=ctx.tenant_id) as session:
        params = resource_route_params(ctx, session)
        if operation == "deployment.list":
            result = await routes.list_deployments(**params, trigger_type=None, enabled=None,
                workflow_id=args.get("workflow_id"), q=None, limit=args.get("limit", 20), offset=args.get("offset", 0))
            items = [{key: deployment_status(item)[key] for key in ("deployment_id", "name", "workflow_id", "trigger_type", "status")} for item in result["items"]]
            offset = args.get("offset", 0) + len(items)
            return {"deployments": items, "next_offset": offset if offset < result.get("total", offset) else None,
                    "message": "Authorized deployments listed. Use info for configuration."}
        dep_id = uuid.UUID(args["deployment_id"])
        dep = await routes.get_deployment(dep_id, **params)
        if operation == "deployment.info":
            return deployment_status(dep)
        await routes._authorize_deployment(deployment_id=dep_id, action=Action.INSPECT_RUNS,
            **{k: v for k, v in params.items() if k != "session"})
        if operation in {"deployment.status", "deployment.logs", "deployment.result"}:
            row = (await session.execute(text("""SELECT id,status,source,trigger_type,submitted_at,started_at,
                finished_at,latency_ms,error,result_summary FROM deployment_invocations
                WHERE deployment_id=:dep AND id=:id"""), {"dep": dep_id, "id": uuid.UUID(args["execution_id"])})).mappings().one_or_none()
            if row is None:
                raise HTTPException(404, "Execution not found in this deployment.")
            result = execution_status(dict(row), dep_id)
            if operation == "deployment.status":
                return jsonable_encoder(result)
            repo = WorkflowHistoryRepo(session)
            run = await repo.get(args["execution_id"])
            if run is not None and (run["source_type"] != "deployment" or str(run["source_id"]) != str(dep_id)):
                raise HTTPException(404, "Execution trace not found in this deployment.")
            if operation == "deployment.result":
                detail = await repo.result_detail(args["execution_id"]) if run else None
                return jsonable_encoder(execution_result(result, detail))
            after = args.get("after", 0)
            frames = await repo.events(args["execution_id"], after=after, limit=args.get("limit", 100)) if run else []
            cursor = frames[-1]["seq"] if frames else after
            return jsonable_encoder({**result, "logs": frames, "cursor": cursor,
                "has_more": bool(run and cursor < run["last_seq"]), "logs_available": run is not None,
                "terminal": row["status"] in {"succeeded", "failed", "timed_out", "cancelled"},
                "hint": f"flowork-cli deployment logs --deployment_id {dep_id} --execution_id {args['execution_id']} --after {cursor}"})
        else:
            start = datetime.fromisoformat(args["from_time"].replace("Z", "+00:00")) if args.get("from_time") else None
            end = datetime.fromisoformat(args["to_time"].replace("Z", "+00:00")) if args.get("to_time") else None
            if args.get("export"):
                end = end or datetime.now(timezone.utc)
                start = start or end - timedelta(days=7)
            page = await routes.history(dep_id, **params, limit=args.get("limit", 20), cursor=args.get("after"),
                status_filter=[args["status"]] if args.get("status") else [], from_=start, to=end, order="desc")
            items = [execution_status(row, dep_id) for row in page["items"]]
            cursor = page["next_cursor"]
            result = {"deployment_id": str(dep_id), "history": items, "next_cursor": cursor,
                      "message": "Invocation summaries listed; these are not full node logs."}
        if args.get("export"):
            result["window"] = {"from": start.isoformat(), "to": end.isoformat()}
            metadata = {key: value for key, value in result.items() if key != "history"}
            result["files"] = {
                "history.json": json.dumps(jsonable_encoder(metadata), ensure_ascii=False),
                "invocations.jsonl": "".join(json.dumps(jsonable_encoder(item), ensure_ascii=False) + "\n" for item in items)}
            result.pop("history", None)
        return jsonable_encoder(result)


async def execute(call, args):
    operation, cap = call.operation, call.capability
    started = False
    try:
        ctx = await agent_context.resolve_context(cap)
        if operation not in WRITE_OPERATIONS:
            return await read(ctx, operation, args)
        dep_id = uuid.UUID(args["deployment_id"]) if "deployment_id" in args else None
        action = {"deployment.delete": Action.DELETE, "deployment.rotate_key": Action.MANAGE_SECRET,
                  "deployment.run": Action.EXECUTE}.get(operation, Action.UPDATE)
        async with session_scope(tenant_id=ctx.tenant_id) as session:
            params = resource_route_params(ctx, session)
            auth_params = {k: v for k, v in params.items() if k != "session"}
            current = None
            if dep_id:
                await routes._authorize_deployment(deployment_id=dep_id, action=action, **auth_params)
                current = await DeploymentsRepo(session).get(dep_id)
                if current is None:
                    raise HTTPException(404, "Deployment not found.")
                if operation == "deployment.rotate_key" and current["trigger_type"] != "api":
                    raise ToolError("unsupported_trigger_type", "rotate_key supports API deployments only. No changes were made.")
            else:
                await routes._authorize_organization_create(**auth_params)
                await routes._authorize_workflow_deploy(workflow_id=args["workflow_id"], **auth_params)
            if operation in {"deployment.create", "deployment.rotate_key"}:
                await step_up(params)
            if cap.approval_mode not in {"agent", "always_ask", "always_allow"}:
                raise ToolError("invalid_approval_mode", "Unknown approval mode.")
            await session.execute(text("""INSERT INTO deployment_cli_leases(call_id,tenant_id,run_id,operation,expires_at)
                VALUES (:id,CAST(:tenant AS uuid),:run,:operation,now()+interval '30 seconds')"""),
                {"id": call.call_id, "tenant": cap.tenant_id, "run": cap.turn_id, "operation": operation})
        call.durable_lease = True
        if cap.approval_mode == "always_ask" or (cap.approval_mode == "agent" and needs_approval(operation, args, current)):
            from .cli_delete import _approve
            summary = {k: v for k, v in args.items() if k != "inputs"}
            if current:
                summary.update(current_name=current["name"], workflow_id=current["wf_id"])
            await _approve(call, summary, prompt="Approve " + operation.replace(".", " ") + "? " + json.dumps(summary) + ". This changes a published entry point or makes a real call. In-flight side effects are not undone.")
            decision = "approved"
        else:
            decision = "auto_approved"
        await call.emit({"progress": {"status": decision, "message": "Operation approved. Rechecking current permission and configuration."}})
        ctx = await agent_context.resolve_context(cap)
        async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
            await _require_active_chat_write(session, ctx)
            if not (await session.execute(text("SELECT 1 FROM deployment_cli_leases WHERE call_id=:id AND expires_at>now()"), {"id": call.call_id})).first():
                raise ToolError("approval_cancelled", "The CLI command is no longer active.")
            params = resource_route_params(ctx, session)
            if dep_id:
                await session.execute(text("SELECT id FROM deployments WHERE id=:id FOR UPDATE"), {"id": dep_id})
                fresh = await DeploymentsRepo(session).get(dep_id)
                keys = ("updated_at", "enabled", "version_pin", "pinned_major", "pinned_sub", "mount_enabled", "rate_limit_qps", "timeout_seconds")
                if fresh is None or any(fresh.get(key) != current.get(key) for key in keys):
                    raise ToolError("state_conflict", "Deployment settings changed while waiting. Inspect info before requesting a new operation.")
            if operation in {"deployment.create", "deployment.rotate_key"}:
                await step_up(params)
            started = True
            if operation == "deployment.create":
                result = await routes.create_deployment(routes.CreateDeploymentBody(**settings(args)), **params, _step_up=params["ctx"])
                dep_id = uuid.UUID(result["id"])
                credential = {key: result[key] for key in ("api_key", "hmac_secret") if key in result}
                # Build from the committed request if authorization projection is pending.
                status = deployment_status({**settings(args), "id": dep_id})
                return {**status, "authorization_pending": bool(result.get("authorization_pending")),
                    "_credential": {"deployment_id": str(dep_id), **credential},
                    "credential_type": "api_key" if args["trigger_type"] == "api" else "hmac_secret",
                    "message": "Deployment created. No workflow was executed. Store the one-time credential securely; do not create again."}
            if operation == "deployment.rotate_key":
                result = await routes.rotate_key(dep_id, **params, _step_up=params["ctx"])
                return {"deployment_id": str(dep_id), "credential_type": "api_key", "_credential": {"deployment_id": str(dep_id), **result},
                        "message": "API key rotated. The old key is invalid. Update callers using the new private credential file."}
            if operation == "deployment.delete":
                await routes.delete_deployment(dep_id, **params)
                return {"deployment_id": str(dep_id), "status": "deleted", "message": "Deployment deleted and disabled. Its Workflow was not deleted. In-flight side effects are not undone."}
            if operation != "deployment.run":
                fields = settings(args) if operation == "deployment.update" else {"enabled": operation == "deployment.enable"}
                result = await routes.patch_deployment(dep_id, routes.PatchDeploymentBody(**fields), **params)
                return deployment_status(result)
            from vibecanvas_api.services.deployment_revisions import admit_revision
            _, active_revision = await admit_revision(session, dep_id)
            snapshot = {"workflow": await resolve_workflow(session, ctx.username, active_revision["spec"]),
                        "mount_enabled": active_revision["spec"]["mount_enabled"]}
        # A live test owns its session independently of the shell's observer.
        # Interrupting observation must not leave history stuck at running.
        async def test():
            async with session_scope(tenant_id=ctx.tenant_id) as session:
                params = resource_route_params(ctx, session)
                params["request"].state.cli_deployment_progress = call.emit
                params["request"].state.cli_deployment_snapshot = snapshot
                return await routes.test_invoke(dep_id, args.get("inputs", {}), **params)
        running = asyncio.create_task(test())
        _running_tests.add(running)
        running.add_done_callback(test_finished)
        result = await asyncio.shield(running)
        return invocation_result(dep_id, result)

    except HTTPException as exc:
        detail = exc.detail
        code = (detail.get("code") if isinstance(detail, dict) else None) or {401: "authentication_required", 403: "permission_denied", 404: "resource_unavailable", 409: "state_conflict", 422: "invalid_arguments"}.get(exc.status_code, "deployment_error")
        return error(code, str(detail), "Inspect deployment info/status/history and this command's --help before retrying. Authentication requirements cannot be bypassed by approval.")
    except ToolError as exc:
        return error(str(exc), exc.message, "Respect approval decisions. Do not automatically resubmit; inspect current permissions and deployment info.")
    except PermissionError:
        return error("permission_denied", "The current identity or command is no longer authorized.", "Use a new authorized Agent turn.")
    except ValueError as exc:
        return error("invalid_arguments", str(exc), "Check this command's --help.")
    except Exception:
        return uncertain_result() if started else error("deployment_unavailable", "The deployment operation is unavailable.", "Inspect info and report the failure; do not retry repeatedly.")


def invocation_result(deployment_id, response):
    """Report accepted work without claiming it has finished successfully."""
    if isinstance(response, JSONResponse):
        result = {**json.loads(response.body), "http_status": response.status_code}
    else:
        result = dict(response)
    result["errors"] = execution_errors(result.get("errors") or {})
    pending = result.get("http_status") == 202 or result["status"] in {"queued", "running", "waiting_approval"}
    if pending:
        message = "Invocation accepted. Inspect this execution's status or detail link; do not submit it again."
    elif result["status"] == "succeeded":
        message = "Test execution succeeded. External API key, signature and network access were not tested."
    else:
        message = "Test execution failed."
    return {
        "deployment_id": str(deployment_id), **result, "message": message,
        "hint": f"flowork-cli deployment result --deployment_id {deployment_id} --execution_id {result['execution_id']}",
    }
