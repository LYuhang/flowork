"""Public HTTP envelopes, including validation before a handler is called."""

import json

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from vibecanvas_api.services.deployment_http import DeploymentPublicRoute
from vibecanvas_api.services.deployment_results import accepted_response, external_result, session_invocation_response


@pytest.mark.parametrize("status,detail,code", [
    (429, "rate limit exceeded", "rate_limit_exceeded"),
    (429, "tenant_concurrency_limit_exceeded", "concurrency_limit_exceeded"),
    (503, "deployment_starting", "deployment_not_ready"),
    (503, "private executor detail", "executor_unavailable"),
    (409, "idempotency_key_conflict", "idempotency_conflict"),
    (422, "approval_assignee_unavailable", "invalid_input"),
])
def test_public_rejection_contract(status, detail, code):
    app = FastAPI()
    router = APIRouter(route_class=DeploymentPublicRoute)

    @router.post("/invoke")
    async def invoke(body: dict):
        raise HTTPException(status, detail)

    app.include_router(router)
    response = TestClient(app).post("/invoke", json={})
    assert response.status_code == status
    assert response.json()["error"] == {"code": code}
    if status in {429, 503}:
        assert int(response.headers["Retry-After"]) > 0
    invalid = TestClient(app).post("/invoke", json=[])
    assert invalid.status_code == 422
    assert invalid.json()["error"] == {"code": "invalid_input"}


def test_unexpected_error_never_exposes_private_details():
    app = FastAPI()
    router = APIRouter(route_class=DeploymentPublicRoute)

    @router.get("/result")
    async def result():
        raise RuntimeError("private credentials")

    app.include_router(router)
    response = TestClient(app).get("/result")
    assert response.status_code == 500
    assert response.json()["error"] == {"code": "internal_error"}
    assert "private" not in response.text


def test_ticket_matches_document_and_session_link_uses_session_authorization():
    response = accepted_response(slug="orders", invocation_id="abc123", state="waiting_approval", async_reason="human_approval")
    ticket = json.loads(response.body)
    assert ticket["status_url"] == ticket["result_url"] == response.headers["Location"]
    assert ticket["poll_after_seconds"] == int(response.headers["Retry-After"]) == 3
    assert ticket["async_reason"] == "human_approval"
    session = session_invocation_response(response)
    body = json.loads(session.body)
    assert body["status_url"] == body["result_url"] == session.headers["Location"] == "/api/v1/workflow-executions/abc123"


def test_terminal_results_use_documented_outputs_and_error():
    detail = {"id": "abc123", "status": "succeeded", "result": {"final_outputs": {"__end__": {"order_status": "submitted"}, "private_node": "secret"}}}
    response = external_result(detail)
    assert response["outputs"] == {"order_status": "submitted"}
    assert response["error"] is None
    assert "secret" not in json.dumps(response)
    response = external_result({**detail, "status": "timed_out"})
    assert response["error"] == {"code": "execution_timeout"}
