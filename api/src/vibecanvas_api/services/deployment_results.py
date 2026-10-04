"""External invocation contract: durable state, final outputs and safe errors.

The browser history endpoint owns graph and node diagnostics. API-key callers
receive only the final EndNode output, never intermediate values or tracebacks.
"""

import json
from urllib.parse import quote

from fastapi.responses import JSONResponse

from vibecanvas_api.storage.workflow_history_repo import TERMINAL_STATUSES


def result_location(slug: str, invocation_id: str) -> str:
    return f"/api/v1/deployments/{quote(slug, safe='')}/runs/{invocation_id}"


def webhook_result_location(slug: str, invocation_id: str) -> str:
    return f"/api/v1/deployments/{quote(slug, safe='')}/webhook/runs/{invocation_id}"


def external_result(detail: dict) -> dict:
    state = detail["status"]
    result = detail.get("result") or {}
    payload = {"invocation_id": str(detail["id"]), "status": state, "error": None}
    if state not in TERMINAL_STATUSES:
        return payload
    payload["exec_time_ms"] = float(result.get("execution_time") or 0) * 1000
    outputs = result.get("final_outputs") or {}
    payload["outputs"] = outputs.get("__end__", {})
    if state != "succeeded":
        code = {
            "timed_out": "execution_timeout",
            "cancelled": "execution_cancelled",
        }.get(state, "execution_failed")
        if detail.get("error_code") in {
            "approval_timeout",
            "execution_lost",
            "execution_dispatch_failed",
            "execution_unavailable",
            "execution_quota_exceeded",
            "execution_resume_failed",
        }:
            code = detail["error_code"]
        code = {"execution_dispatch_failed": "internal_error", "execution_unavailable": "executor_unavailable",
                "execution_quota_exceeded": "concurrency_limit_exceeded"}.get(code, code)
        payload.update(error={"code": code}, error_code=code, errors={"__top__": code})
    return payload


def accepted_response(*, slug: str, invocation_id: str, state: str = "queued", webhook: bool = False, async_reason: str = "explicit_async") -> JSONResponse:
    location = (webhook_result_location if webhook else result_location)(slug, invocation_id)
    return JSONResponse(
        status_code=202,
        headers={"Location": location, "Retry-After": "3"},
        content={"invocation_id": invocation_id, "task_id": invocation_id, "status": state,
                 "status_url": location, "result_url": location, "async_reason": async_reason, "poll_after_seconds": 3},
    )


def sync_result_response(detail: dict):
    payload = external_result(detail)
    if detail["status"] == "succeeded":
        return payload
    code = 504 if detail["status"] == "timed_out" else 502
    code = {"execution_dispatch_failed": 500, "execution_unavailable": 503, "execution_quota_exceeded": 429}.get(
        detail.get("error_code"), code
    )
    return JSONResponse(status_code=code, content=payload, headers={"Retry-After": "1"} if code in {429, 503} else None)


def session_invocation_response(response):
    """Use session-authorized history links for dashboard and CLI callers."""
    payload = json.loads(response.body) if isinstance(response, JSONResponse) else dict(response)
    execution_id = payload["invocation_id"]
    location = f"/api/v1/workflow-executions/{execution_id}"
    payload.update(
        execution_id=execution_id,
        execution_url=f"/workflow-executions/{execution_id}",
        result_url=location,
        status_url=location,
    )
    if isinstance(response, JSONResponse):
        headers = {"Location": location}
        if "retry-after" in response.headers:
            headers["Retry-After"] = response.headers["retry-after"]
        return JSONResponse(status_code=response.status_code, content=payload, headers=headers)
    return payload
