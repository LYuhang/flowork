"""Public deployment errors use one stable envelope at the HTTP boundary.

Keep internal exception details out of API-key/webhook responses. Session APIs
and the shared admission services retain their existing exception contracts.
"""

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException
import structlog

logger = structlog.get_logger(__name__)


def rejection_response(status: int, detail=None, headers=None) -> JSONResponse:
    code = {
        400: "invalid_input", 401: "unauthorized", 403: "forbidden",
        404: "not_found", 409: "idempotency_conflict", 413: "payload_too_large",
        415: "unsupported_media_type", 422: "invalid_input",
        429: "concurrency_limit_exceeded", 503: "executor_unavailable",
    }.get(status, "internal_error")
    if status == 429 and detail == "rate limit exceeded":
        code = "rate_limit_exceeded"
    if status == 503 and detail in ("deployment_starting", "deployment_not_ready"):
        code = "deployment_not_ready"
    if status == 503 and detail == "rate_limit_unavailable":
        code = "rate_limit_unavailable"
    response_headers = dict(headers or {})
    if status in {429, 503}:
        response_headers.setdefault("Retry-After", "1")
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code}, "error_code": code, "detail": code},
        headers=response_headers,
    )


class DeploymentPublicRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                return await original(request)
            except HTTPException as exc:
                return rejection_response(exc.status_code, exc.detail, exc.headers)
            except RequestValidationError:
                return rejection_response(422)
            except Exception as exc:
                # Validation and dependency errors can embed caller data or
                # secrets; log only the exception type, never its message.
                logger.error("deployment_http_failed", error_type=type(exc).__name__)
                return rejection_response(500)

        return handle
