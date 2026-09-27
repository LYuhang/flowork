"""Read-only Workflow configuration queries for the Agent CLI."""
from copy import deepcopy

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.services.agent_resources.workflow_transfer import read_workflow_snapshot
from vibecanvas_api.services.workflow_model_policy import models_for_context
from vibecanvas_engine.nodes.config import get_code_timeout
from vibecanvas_engine.workflow import DEFAULT_WORKFLOW_TIMEOUT
from vibecanvas_engine.nodes.http_request import DEFAULT_HTTP_TIMEOUT


def public_settings(graph: dict) -> dict:
    """Only the settings owned by the canvas; no legacy tool/secret config."""
    settings = (graph.get("__meta__") or {}).get("settings") or {}
    if not isinstance(settings, dict):
        raise ToolError("invalid_workflow", "Workflow settings must be an object.")
    result = {}
    if isinstance(settings.get("timeouts"), dict):
        result["timeouts"] = {k: v for k, v in settings["timeouts"].items() if k in {"workflow", "code", "http"} and type(v) in (int, float)}
    if isinstance(settings.get("code_requirements"), str):
        result["code_requirements"] = settings["code_requirements"]
    egress = settings.get("egress")
    if isinstance(egress, dict) and isinstance(egress.get("allowed_hosts"), list):
        result["egress"] = {"allowed_hosts": [host for host in egress["allowed_hosts"] if isinstance(host, str)]}
    return deepcopy(result)


async def get_config(ctx, arguments: dict) -> dict:
    if arguments["scope"] == "model_api":
        models = await models_for_context(ctx)
        if not models:
            return {
                "models": {},
                "message": "No eligible Workflow model APIs are available to the current user.",
                "hint": (
                    "Ask the user to add or enable a manually configured API in Settings > API credentials "
                    "and grant use permission, then rerun this command. Chat account connections are not "
                    "Workflow APIs. Do not invent a model key or substitute keyword rules for requested "
                    "model analysis without the user's agreement."
                ),
            }
        return {"models": models}
    snapshot = await read_workflow_snapshot(ctx, workflow_id=arguments["workflow_id"], major=arguments["major"])
    return {
        "id": snapshot["id"], "version": snapshot["version"],
        "settings": public_settings(snapshot["workflow"]),
        "defaults": {"timeouts": {"workflow": DEFAULT_WORKFLOW_TIMEOUT, "code": get_code_timeout(), "http": DEFAULT_HTTP_TIMEOUT}},
    }
