from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from fastapi import HTTPException
from sqlalchemy import text, update
from starlette.requests import Request

from vibecanvas_api.app import build_app
from vibecanvas_api.auth.tokens import hash_token
from vibecanvas_api.config import config
from vibecanvas_api.routes.runtime_model_broker import (
    _custom_tool_history_rejected,
    _extract_capability,
    _flatten_namespace_tools,
    _forward_headers,
    _model_path_allowed,
    _missing_input_status_rejected,
    _input_message_phase_rejected,
    _namespace_tools_rejected,
    _reasoning_summary_rejected,
    _requires_eager_namespace_compatibility,
    _responses_request_shape,
    _SseShapeObserver,
    _rewrite_namespace_response_json,
    _rewrite_namespace_sse_line,
    _target_url,
    _unsupported_optional_request_field,
    _validated_user_destination,
    _validate_requested_model,
    _web_search_external_access_rejected,
    _with_completed_assistant_status,
    _with_function_compatible_custom_tool_history,
    _with_openrouter_server_tools,
    _without_reasoning_summary,
    _without_input_message_phase,
    _without_optional_request_fields,
    _without_web_search_external_access,
)
from vibecanvas_api.services.agent_runtime.model_capability import (
    authorization_model_generation,
    mint_runtime_model_capability,
    model_config_revision,
    verify_runtime_model_capability,
)
from vibecanvas_api.services.agent_runtime.workflow_model_capability import (
    mint_runtime_workflow_model_capability,
    verify_runtime_workflow_model_capability,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.models import Session
from vibecanvas_api.storage.models_privileged_access import (
    PlatformAdminEligibility,
)


def _token(*, secret: str = "s" * 64, now: int = 1000) -> str:
    return mint_runtime_model_capability(
        organization_id="org-1",
        user_id="user-1",
        chat_id="chat-1",
        turn_id="turn-1",
        runtime_session_id="runtime-1",
        session_id="session-1",
        session_generation=7,
        membership_id="membership-1",
        credential_id="credential-1",
        provider="openai",
        model="gpt-test",
        config_revision="r" * 64,
        authorization_generation="a" * 64,
        resources=("chat:chat-1", "llm_credential:credential-1"),
        actions=("chat:execute", "model:invoke", "llm_credential:use"),
        secret=secret,
        ttl_s=120,
        now=now,
    )


def _request(*, headers: dict[str, str], query: bytes = b"") -> Request:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/internal/runtime-model/v1/chat/completions",
            "raw_path": b"/api/internal/runtime-model/v1/chat/completions",
            "query_string": query,
            "headers": raw_headers,
            "client": ("127.0.0.1", 1234),
            "server": ("platform.test", 443),
        }
    )


def _cookie_csrf(client: AsyncClient, audience: str = "web") -> dict[str, str]:
    csrf = client.cookies.get(f"vibecanvas-{audience}-csrf")
    assert csrf
    return {"Origin": "http://testserver", "X-CSRF-Token": csrf}


async def _seed_operator_eligibilities(first: str, second: str) -> None:
    now = datetime.now(timezone.utc)
    async with session_scope() as session:
        session.add_all([
            PlatformAdminEligibility(
                platform_user_id=uuid.UUID(first),
                role="platform_support",
                status="active",
                granted_by_user_id=uuid.UUID(second),
                reviewed_by_user_id=uuid.UUID(second),
                reviewed_at=now,
                expires_at=now + timedelta(days=30),
            ),
            PlatformAdminEligibility(
                platform_user_id=uuid.UUID(second),
                role="platform_support",
                status="active",
                granted_by_user_id=uuid.UUID(first),
                reviewed_by_user_id=uuid.UUID(first),
                reviewed_at=now,
                expires_at=now + timedelta(days=30),
            ),
        ])


async def _grant_webauthn(raw_session: str) -> None:
    async with session_scope() as session:
        await session.execute(
            update(Session)
            .where(Session.token_hash == hash_token(raw_session))
            .values(
                authentication_strength="webauthn",
                step_up_expires_at=(
                    datetime.now(timezone.utc) + timedelta(minutes=10)
                ),
            )
        )


def test_runtime_model_capability_is_scoped_signed_and_expiring():
    token = _token()
    capability = verify_runtime_model_capability(token, secret="s" * 64, now=1050)
    assert capability is not None
    assert capability.audience == "runtime-model"
    assert capability.session_generation == 7
    assert capability.resources == (
        "chat:chat-1",
        "llm_credential:credential-1",
    )
    assert capability.actions == (
        "chat:execute",
        "llm_credential:use",
        "model:invoke",
    )
    assert verify_runtime_model_capability(
        token + "x", secret="s" * 64, now=1050
    ) is None
    assert verify_runtime_model_capability(
        token, secret="wrong", now=1050
    ) is None
    assert verify_runtime_model_capability(
        token, secret="s" * 64, now=1120
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/v1",
        "http://[::1]:8000/v1",
        "http://model.example.test/v1",
        "https://api.openai.com/v1",
    ],
)
async def test_host_model_egress_accepts_every_host_reachable_http_endpoint(
    monkeypatch,
    url,
):
    monkeypatch.setattr(config, "runtime_model_egress_policy", "host")

    resolved, hostname, addresses = await _validated_user_destination(
        url,
        label="model API URL",
    )

    assert resolved == url
    assert hostname
    assert addresses == ()


@pytest.mark.asyncio
async def test_public_https_model_egress_remains_an_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(config, "runtime_model_egress_policy", "public_https")

    with pytest.raises(HTTPException) as exc_info:
        await _validated_user_destination(
            "http://127.0.0.1:8000/v1",
            label="model API URL",
        )

    assert exc_info.value.detail["code"] == "runtime_model_destination_unsafe"


def test_runtime_model_capability_payload_never_contains_provider_secret():
    token = _token()
    payload = json.loads(
        __import__("base64").urlsafe_b64decode(
            token.split(".", 1)[0] + "=="
        )
    )
    assert "api_key" not in payload
    assert "base_url" not in payload
    assert "proxy" not in payload
    assert payload["res"] == ["chat:chat-1", "llm_credential:credential-1"]


def test_runtime_model_capability_cannot_select_a_different_model():
    _validate_requested_model(
        body=b'{"model":"gpt-allowed","input":[]}',
        path="responses",
        provider="openai",
        allowed_model="gpt-allowed",
    )
    with pytest.raises(HTTPException) as exc_info:
        _validate_requested_model(
            body=b'{"model":"gpt-other","input":[]}',
            path="responses",
            provider="openai",
            allowed_model="gpt-allowed",
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "runtime_model_selection_denied"


def test_responses_request_shape_never_includes_content_or_arguments():
    shape = _responses_request_shape(json.dumps({
        "model": "gpt-test",
        "input": [
            {"type": "message", "content": "private prompt"},
            {"type": "function_call", "arguments": "private arguments"},
        ],
        "tools": [
            {"type": "function", "name": "private_name", "description": "private"},
            {"type": "local_shell"},
        ],
        "metadata": {"private": "value"},
        "stream": True,
    }).encode())

    assert shape == {
        "json": True,
        "keys": ["input", "metadata", "model", "stream", "tools"],
        "tool_types": {"function": 1, "local_shell": 1},
        "input_types": {"function_call": 1, "message": 1},
        "stream": True,
    }
    assert "private" not in repr(shape)


def test_sse_shape_observer_counts_only_event_and_item_discriminators():
    observer = _SseShapeObserver()
    private = json.dumps({
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "name": "private_tool_name",
            "arguments": "private arguments",
        },
    }).encode()
    observer.feed(b"data: " + private[:23])
    observer.feed(private[23:] + b"\n\ndata: [DONE]\n\n")

    assert observer.summary() == {
        "event_types": {"[DONE]": 1, "response.output_item.done": 1},
        "item_types": {"function_call": 1},
        "failed_codes": {},
        "chunks": 2,
        "bytes": len(b"data: " + private + b"\n\ndata: [DONE]\n\n"),
        "oversized_lines": 0,
    }
    assert "private" not in repr(observer.summary())


def test_sse_shape_observer_exposes_only_bounded_failure_discriminators():
    observer = _SseShapeObserver()
    observer.feed(b'data: {"type":"response.failed","response":{"error":'
                  b'{"code":"server_error","type":"provider_error",'
                  b'"message":"private prompt must never be logged"}}}\n\n')

    summary = observer.summary()

    assert summary["failed_codes"] == {
        "code:server_error": 1,
        "type:provider_error": 1,
    }
    assert "private" not in repr(summary)


def test_namespace_compatibility_preserves_tools_and_multistep_calls():
    body = json.dumps(
        {
            "model": "gpt-test",
            "input": [
                {
                    "type": "function_call",
                    "namespace": "mcp__canvas",
                    "name": "render",
                    "call_id": "call-1",
                    "arguments": "{}",
                }
            ],
            "tools": [
                {"type": "function", "name": "shell", "parameters": {}},
                {
                    "type": "namespace",
                    "name": "mcp__canvas",
                    "description": "Canvas tools",
                    "tools": [
                        {
                            "type": "function",
                            "name": "render",
                            "description": "Render an artifact",
                            "parameters": {"type": "object"},
                            "strict": False,
                        },
                        {
                            "type": "function",
                            "name": "save",
                            "parameters": {"type": "object"},
                        },
                    ],
                },
                {"type": "web_search", "external_web_access": True},
            ],
        }
    ).encode()

    rewrite = _flatten_namespace_tools(body)

    assert rewrite is not None
    payload = json.loads(rewrite.body)
    assert [tool["type"] for tool in payload["tools"]] == [
        "function",
        "function",
        "function",
        "web_search",
    ]
    assert [tool.get("name") for tool in payload["tools"]] == [
        "shell",
        "mcp__canvas__render",
        "mcp__canvas__save",
        None,
    ]
    assert payload["input"][0]["name"] == "mcp__canvas__render"
    assert "namespace" not in payload["input"][0]
    assert rewrite.flat_to_namespaced == {
        "mcp__canvas__render": ("mcp__canvas", "render"),
        "mcp__canvas__save": ("mcp__canvas", "save"),
    }


def test_openrouter_eagerly_uses_lossless_namespace_compatibility():
    assert _requires_eager_namespace_compatibility("openrouter")
    assert _requires_eager_namespace_compatibility("Open-Router")
    assert not _requires_eager_namespace_compatibility("openai")


def test_openrouter_responses_projects_only_the_hosted_search_descriptor():
    body = json.dumps({
        "model": "stealth/ox-alpha",
        "tools": [
            {"type": "function", "name": "shell", "parameters": {}},
            {
                "type": "web_search",
                "external_web_access": True,
                "search_context_size": "medium",
            },
        ],
    }).encode()

    projected = json.loads(_with_openrouter_server_tools(body))

    assert projected["tools"] == [
        {"type": "function", "name": "shell", "parameters": {}},
        {"type": "openrouter:web_search"},
    ]


def test_namespace_compatibility_restores_json_and_sse_function_calls():
    mapping = {"mcp__canvas__render": ("mcp__canvas", "render")}
    event = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "name": "mcp__canvas__render",
            "call_id": "call-1",
            "arguments": "{}",
        },
    }

    restored = json.loads(
        _rewrite_namespace_response_json(
            json.dumps(event).encode(),
            flat_to_namespaced=mapping,
        )
    )
    assert restored["item"]["namespace"] == "mcp__canvas"
    assert restored["item"]["name"] == "render"

    line = b"data: " + json.dumps(event).encode() + b"\r\n"
    restored_line = _rewrite_namespace_sse_line(
        line,
        flat_to_namespaced=mapping,
    )
    assert restored_line.endswith(b"\r\n")
    restored_sse = json.loads(restored_line.split(b":", 1)[1].strip())
    assert restored_sse["item"]["namespace"] == "mcp__canvas"
    assert restored_sse["item"]["name"] == "render"
    assert _rewrite_namespace_sse_line(
        b"data: [DONE]\n",
        flat_to_namespaced=mapping,
    ) == b"data: [DONE]\n"


def test_namespace_compatibility_retries_only_explicit_provider_rejection():
    assert _namespace_tools_rejected(
        400,
        b'{"error":{"message":"unknown tool type: namespace",'
        b'"param":"tools[8].type"}}',
    )
    assert not _namespace_tools_rejected(
        400,
        b'{"error":{"message":"invalid namespace tool schema"}}',
    )
    assert not _namespace_tools_rejected(
        500,
        b'{"error":{"message":"unknown tool type: namespace"}}',
    )


def test_custom_tool_history_compatibility_preserves_input_and_call_pairing():
    source = json.dumps({
        "model": "gpt-test",
        "input": [
            {
                "type": "custom_tool_call",
                "id": "ctc-1",
                "call_id": "call-1",
                "name": "apply_patch",
                "input": "*** Begin Patch\n原始内容\n*** End Patch",
                "status": "completed",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": "Done!",
            },
            {"type": "message", "role": "user", "content": []},
        ],
    }).encode()

    rewritten = json.loads(_with_function_compatible_custom_tool_history(source))

    call, output, message = rewritten["input"]
    assert call["type"] == "function_call"
    assert call["call_id"] == "call-1"
    assert call["name"] == "apply_patch"
    assert json.loads(call["arguments"]) == {
        "input": "*** Begin Patch\n原始内容\n*** End Patch"
    }
    assert "input" not in call
    assert output == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "Done!",
    }
    assert message == {"type": "message", "role": "user", "content": []}
    assert _custom_tool_history_rejected(
        400,
        b'{"error":{"message":"unknown type: custom_tool_call",'
        b'"param":"input.type"}}',
    )
    assert not _custom_tool_history_rejected(
        400,
        b'{"error":{"message":"invalid custom_tool_call output"}}',
    )
    assert not _custom_tool_history_rejected(
        500,
        b'{"error":{"message":"unknown type: custom_tool_call",'
        b'"param":"input.type"}}',
    )


def test_web_search_compatibility_removes_only_rejected_optional_hint():
    body = json.dumps(
        {
            "model": "gpt-test",
            "tools": [
                {"type": "function", "name": "shell", "parameters": {}},
                {"type": "web_search", "external_web_access": True},
            ],
        }
    ).encode()

    payload = json.loads(_without_web_search_external_access(body))

    assert payload["tools"] == [
        {"type": "function", "name": "shell", "parameters": {}},
        {"type": "web_search"},
    ]
    assert _web_search_external_access_rejected(
        400,
        b'{"message":"json: unknown field \\"external_web_access\\""}',
    )
    assert not _web_search_external_access_rejected(
        400,
        b'{"message":"unknown tool type: web_search"}',
    )


def test_responses_input_status_compatibility_is_bounded_and_lossless():
    body = json.dumps(
        {
            "model": "gpt-test",
            "input": [
                {
                    "id": "developer-1",
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "rules"}],
                },
                {
                    "id": "reasoning-1",
                    "type": "reasoning",
                    "summary": [],
                    "encrypted_content": "opaque",
                },
                {
                    "id": "assistant-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "incomplete",
                    "content": [],
                },
            ],
        }
    ).encode()

    body_payload = json.loads(body)
    body_payload["input"].append(
        {
            "id": "assistant-2",
            "type": "message",
            "role": "assistant",
            "content": [],
        }
    )
    rewritten = json.loads(
        _with_completed_assistant_status(json.dumps(body_payload).encode())
    )

    assert "status" not in rewritten["input"][0]
    assert "status" not in rewritten["input"][1]
    assert rewritten["input"][2]["status"] == "incomplete"
    assert rewritten["input"][3]["status"] == "completed"
    assert rewritten["input"][0]["content"][0]["text"] == "rules"
    assert rewritten["input"][1]["encrypted_content"] == "opaque"
    assert _missing_input_status_rejected(
        400,
        b'{"error":{"code":"MissingParameter",'
        b'"param":"input.status","message":"missing input.status"}}',
    )
    assert not _missing_input_status_rejected(
        500,
        b'{"param":"input.status","message":"missing input.status"}',
    )


def test_responses_input_phase_compatibility_is_bounded_and_lossless():
    body = json.dumps(
        {
            "model": "gpt-test",
            "phase": "top-level-must-stay",
            "input": [
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "working"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "done"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "phase": "user-extension-must-stay",
                    "content": [{"type": "input_text", "text": "continue"}],
                },
                {
                    "type": "reasoning",
                    "phase": "reasoning-extension-must-stay",
                    "summary": [],
                },
            ],
        }
    ).encode()

    payload = json.loads(_without_input_message_phase(body))

    assert payload["phase"] == "top-level-must-stay"
    assert "phase" not in payload["input"][0]
    assert "phase" not in payload["input"][1]
    assert payload["input"][0]["status"] == "completed"
    assert payload["input"][0]["content"][0]["text"] == "working"
    assert payload["input"][2]["phase"] == "user-extension-must-stay"
    assert payload["input"][3]["phase"] == "reasoning-extension-must-stay"
    assert _input_message_phase_rejected(
        400,
        b'{"error":{"message":"json: unknown field \\"phase\\""}}',
    )
    assert not _input_message_phase_rejected(
        500,
        b'{"error":{"message":"json: unknown field \\"phase\\""}}',
    )
    assert not _input_message_phase_rejected(
        400,
        b'{"error":{"message":"phase value is invalid"}}',
    )


def test_reasoning_compatibility_keeps_effort_when_summary_is_rejected():
    body = json.dumps(
        {
            "model": "gpt-test",
            "reasoning": {"effort": "high", "summary": "auto"},
        }
    ).encode()

    payload = json.loads(_without_reasoning_summary(body))

    assert payload["reasoning"] == {"effort": "high"}
    assert _reasoning_summary_rejected(
        400,
        b'{"message":"json: unknown field \\"summary\\""}',
    )
    assert not _reasoning_summary_rejected(
        400,
        b'{"message":"invalid reasoning effort"}',
    )


def test_optional_request_compatibility_is_strictly_allowlisted():
    body = json.dumps(
        {
            "model": "gpt-test",
            "client_metadata": {"installation_id": "local"},
            "prompt_cache_key": "cache-key",
            "service_tier": "priority",
            "input": [],
        }
    ).encode()

    payload = json.loads(
        _without_optional_request_fields(
            body,
            {"client_metadata", "service_tier"},
        )
    )

    assert "client_metadata" not in payload
    assert payload["prompt_cache_key"] == "cache-key"
    assert payload["service_tier"] == "priority"
    assert _unsupported_optional_request_field(
        400,
        b'{"message":"json: unknown field \\"client_metadata\\""}',
    ) == "client_metadata"
    assert _unsupported_optional_request_field(
        400,
        b'{"message":"json: unknown field \\"service_tier\\""}',
    ) is None


def test_workflow_model_capability_is_execution_scoped_and_secret_free():
    token = mint_runtime_workflow_model_capability(
        organization_id="org-1",
        user_id="user-1",
        workflow_id="wf-1",
        execution_id="execution-1",
        execution_resource_type="workflow_execution",
        credential_id="credential-1",
        provider="openai",
        model="gpt-test",
        config_revision="r" * 64,
        authorization_generation="a" * 64,
        secret="s" * 64,
        ttl_s=120,
        now=1000,
    )
    capability = verify_runtime_workflow_model_capability(
        token,
        secret="s" * 64,
        now=1050,
    )
    assert capability is not None
    assert capability.audience == "runtime-workflow-model"
    assert capability.workflow_id == "wf-1"
    assert capability.execution_id == "execution-1"
    assert capability.execution_resource_type == "workflow_execution"
    assert capability.credential_id == "credential-1"
    assert verify_runtime_workflow_model_capability(
        token + "x", secret="s" * 64, now=1050
    ) is None
    assert verify_runtime_workflow_model_capability(
        token, secret="s" * 64, now=1120
    ) is None
    payload = json.loads(
        __import__("base64").urlsafe_b64decode(
            token.split(".", 1)[0] + "=="
        )
    )
    assert "api_key" not in payload
    assert "base_url" not in payload
    assert "proxy" not in payload
    assert payload["res"] == [
        "llm_credential:credential-1",
        "workflow:wf-1",
        "workflow_execution:execution-1",
    ]


def test_service_account_workflow_capability_is_generation_fenced():
    account_id = "11111111-1111-4111-8111-111111111111"
    token = mint_runtime_workflow_model_capability(
        organization_id="org-1",
        user_id="user-1",
        workflow_id="wf-1",
        execution_id="task-1",
        execution_resource_type="task",
        credential_id=None,
        provider="openai",
        model="gpt-test",
        config_revision="r" * 64,
        authorization_generation="a" * 64,
        secret="s" * 64,
        ttl_s=120,
        now=1000,
        principal_type="service_account",
        principal_id=account_id,
        principal_generation=7,
    )
    capability = verify_runtime_workflow_model_capability(
        token,
        secret="s" * 64,
        now=1050,
    )
    assert capability is not None
    assert capability.principal_type == "service_account"
    assert capability.principal_id == account_id
    assert capability.principal_generation == 7
    payload = json.loads(
        __import__("base64").urlsafe_b64decode(
            token.split(".", 1)[0] + "=="
        )
    )
    assert f"service_account:{account_id}" in payload["res"]
    assert "service_account:act_as" in payload["act"]
    with pytest.raises(ValueError, match="generation"):
        mint_runtime_workflow_model_capability(
            organization_id="org-1",
            user_id="user-1",
            workflow_id="wf-1",
            execution_id="task-1",
            execution_resource_type="task",
            credential_id=None,
            provider="openai",
            model="gpt-test",
            config_revision="r" * 64,
            authorization_generation="a" * 64,
            secret="s" * 64,
            ttl_s=120,
            principal_type="service_account",
            principal_id=account_id,
            principal_generation=0,
        )


def test_runtime_model_metadata_generations_are_stable_and_secret_free():
    assert model_config_revision(
        provider="OpenAI", model="gpt-test", updated_at="v1"
    ) == model_config_revision(
        provider="openai", model="gpt-test", updated_at="v1"
    )
    assert model_config_revision(
        provider="openai", model="gpt-test", updated_at="v1"
    ) != model_config_revision(
        provider="openai", model="gpt-test", updated_at="v2"
    )
    assert authorization_model_generation(
        model_id="model-1"
    ) != authorization_model_generation(
        model_id="model-2"
    )


def test_provider_credential_headers_extract_the_same_capability():
    token = _token()
    for headers, query in (
        ({"Authorization": f"Bearer {token}"}, b""),
        ({"api-key": token}, b""),
        ({"x-api-key": token}, b""),
        ({"x-goog-api-key": token}, b""),
    ):
        assert _extract_capability(_request(headers=headers, query=query)) == token


def test_query_capability_is_rejected_to_keep_it_out_of_access_logs():
    with pytest.raises(HTTPException) as exc_info:
        _extract_capability(
            _request(headers={}, query=f"key={_token()}".encode())
        )
    assert getattr(exc_info.value, "status_code", None) == 401


def test_model_proxy_strips_capability_and_injects_real_provider_key():
    token = _token()
    request = _request(
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Cookie": "must-not-forward=1",
            "X-Forwarded-For": "10.0.0.1",
            "OpenAI-Beta": "responses=v1",
        }
    )
    headers = _forward_headers(request, provider="openai", api_key="real-secret")
    assert headers["Authorization"] == "Bearer real-secret"
    assert headers["content-type"] == "application/json"
    assert headers["openai-beta"] == "responses=v1"
    assert "cookie" not in {name.casefold() for name in headers}
    assert "x-forwarded-for" not in {name.casefold() for name in headers}
    assert token not in repr(headers)


def test_model_proxy_allows_only_inference_paths_and_strips_query_key():
    assert _model_path_allowed("openai", "chat/completions")
    assert _model_path_allowed("openai", "responses")
    assert _model_path_allowed("anthropic", "v1/messages")
    assert _model_path_allowed(
        "google_genai", "v1beta/models/gemini:streamGenerateContent"
    )
    assert not _model_path_allowed("openai", "models")
    assert not _model_path_allowed("openai", "files")
    assert not _model_path_allowed("openai", "../admin")

    request = _request(headers={}, query=b"key=capability&stream=true")
    target = _target_url(
        "https://provider.test/api/v1?region=us",
        "chat/completions",
        request,
    )
    assert target == (
        "https://provider.test/api/v1/chat/completions?region=us&stream=true"
    )


@pytest.mark.asyncio
async def test_chat_runtime_broker_injects_provider_key_only_on_host(
    client,
    pg_engine,
    monkeypatch,
):
    from vibecanvas_api.config import config
    from vibecanvas_api.routes import chats as chats_route

    upstream_request: dict[str, object] = {}

    async def upstream_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        header_blob = await reader.readuntil(b"\r\n\r\n")
        head, initial = header_blob.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            name, value = line.split(":", 1)
            headers[name.casefold()] = value.strip()
        content_length = int(headers.get("content-length", "0"))
        body = bytearray(initial)
        if len(body) < content_length:
            body.extend(await reader.readexactly(content_length - len(body)))
        upstream_request.update(
            request_line=lines[0],
            headers=headers,
            body=bytes(body),
        )
        payload = b'{"id":"host-brokered","choices":[]}'
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + payload
        )
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
    port = upstream.sockets[0].getsockname()[1]
    monkeypatch.setattr(
        config,
        "codex_managed_apis",
        [{
            "id": "host-broker-test",
            "name": "Host broker test",
            "base_url": f"http://127.0.0.1:{port}/v1",
            "api_key": "provider-secret-on-host",
            "models": ("gpt-host-broker-test",),
        }],
    )

    dispatched_turns = []

    class FakeRuntimeOrchestrator:
        async def stream_turn(self, **kwargs):
            dispatched_turns.append(kwargs["turn_request"])
            yield ("NO_OP", {})

    monkeypatch.setattr(chats_route, "AgentRuntimeOrchestrator", FakeRuntimeOrchestrator)
    email = f"broker_{uuid.uuid4().hex[:12]}@example.com"
    register = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": "Broker User", "password": "pw12345678"},
    )
    assert register.status_code in (200, 201), register.text
    browser_headers = {"Authorization": f"Bearer {register.json()['session_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=browser_headers)).json()
    scope_id = (
        await client.get("/api/v1/chats/bootstrap", headers=browser_headers)
    ).json()["carrier_scope_id"]
    sent = await client.post(
        f"/api/v1/chat-scopes/{scope_id}/chats/chat_broker/messages",
        json={"role": "user", "content": "hello"},
        headers=browser_headers,
    )
    assert sent.status_code == 200, sent.text
    assert len(dispatched_turns) == 1
    turn = dispatched_turns[0]
    capability = turn.model["api_key"]
    assert capability != "provider-secret-on-host"
    assert "provider-secret-on-host" not in repr(turn.model)

    # The fake Runtime completed immediately; reopen the durable Run to model
    # an in-flight provider call and exercise the broker's live Run fence.
    async with pg_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, false)"),
            {"tenant_id": me["tenant_id"]},
        )
        await connection.execute(
            text("UPDATE agent_runs SET status='running' WHERE run_id=:run_id"),
            {"run_id": turn.turn_id},
        )

    try:
        response = await client.post(
            "/api/internal/runtime-model/v1/chat/completions?stream=true",
            headers={
                "Authorization": f"Bearer {capability}",
                "Content-Type": "application/json",
                "Cookie": "must-not-forward=1",
            },
            json={"model": "gpt-host-broker-test", "messages": []},
        )
        assert response.status_code == 200, response.text
        assert response.json()["id"] == "host-brokered"
        assert upstream_request["request_line"] == "POST /v1/chat/completions?stream=true HTTP/1.1"
        upstream_headers = upstream_request["headers"]
        assert isinstance(upstream_headers, dict)
        assert upstream_headers["authorization"] == "Bearer provider-secret-on-host"
        assert "cookie" not in upstream_headers
        assert capability not in repr(upstream_request)
    finally:
        upstream.close()
        await upstream.wait_closed()

    # Rotating the browser Session generation immediately invalidates the
    # already-issued Runtime lease before another provider request is sent.
    async with pg_engine.begin() as connection:
        await connection.execute(
            text("UPDATE sessions SET generation=generation+1 WHERE user_id=:user_id"),
            {"user_id": me["user_id"]},
        )
    denied = await client.post(
        "/api/internal/runtime-model/v1/chat/completions",
        headers={"Authorization": f"Bearer {capability}"},
        json={"model": "gpt-host-broker-test", "messages": []},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "runtime_model_session_revoked"


@pytest.mark.asyncio
async def test_privileged_chat_runtime_broker_keeps_exact_scope_and_revokes(
    pg_engine,
    openfga_allow_all,
    monkeypatch,
):
    """A support Session retains Chat/model capability without widening it."""
    from vibecanvas_api.routes import chats as chats_route
    from vibecanvas_api.routes import privileged_access as privileged_routes

    upstream_calls: list[dict[str, object]] = []

    async def upstream_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        header_blob = await reader.readuntil(b"\r\n\r\n")
        head, initial = header_blob.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, value = line.split(":", 1)
            headers[name.casefold()] = value.strip()
        content_length = int(headers.get("content-length", "0"))
        body = bytearray(initial)
        if len(body) < content_length:
            body.extend(await reader.readexactly(content_length - len(body)))
        upstream_calls.append({"headers": headers, "body": bytes(body)})
        payload = b'{"id":"privileged-host-brokered","choices":[]}'
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + payload
        )
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
    port = upstream.sockets[0].getsockname()[1]
    monkeypatch.setattr(config, "web_session_cookie_enabled", True)
    monkeypatch.setattr(config.public_urls, "public_url", "")
    monkeypatch.setattr(
        config,
        "codex_managed_apis",
        [{
            "id": "support-broker-test",
            "name": "Support broker test",
            "base_url": f"http://127.0.0.1:{port}/v1",
            "api_key": "support-provider-secret",
            "models": ("gpt-support-broker-test",),
        }],
    )
    monkeypatch.setattr(
        privileged_routes,
        "get_email_sender",
        lambda: type(
            "NoopEmailSender",
            (),
            {"send": staticmethod(lambda _to, _subject, _body: None)},
        )(),
    )

    dispatched_turns = []

    class FakeRuntimeOrchestrator:
        async def stream_turn(self, **kwargs):
            dispatched_turns.append(kwargs["turn_request"])
            yield ("NO_OP", {})

    monkeypatch.setattr(
        chats_route,
        "AgentRuntimeOrchestrator",
        FakeRuntimeOrchestrator,
    )
    app = build_app()
    app.state.openfga_client = openfga_allow_all
    transport = ASGITransport(app=app)
    origin = "http://testserver"
    try:
        async with (
            AsyncClient(transport=transport, base_url=origin) as operator,
            AsyncClient(transport=transport, base_url=origin) as approver,
        ):
            operator_registration = await operator.post(
                "/api/v1/auth/register",
                headers={"Origin": origin},
                json={
                    "email": f"support-model-{uuid.uuid4().hex[:10]}@example.com",
                    "username": "Support model operator",
                    "password": "pw12345678",
                },
            )
            approver_registration = await approver.post(
                "/api/v1/auth/register",
                headers={"Origin": origin},
                json={
                    "email": f"support-model-approver-{uuid.uuid4().hex[:10]}@example.com",
                    "username": "Support model approver",
                    "password": "pw12345678",
                },
            )
            assert operator_registration.status_code == 201
            assert approver_registration.status_code == 201
            operator_id = operator_registration.json()["user"]["user_id"]
            approver_id = approver_registration.json()["user"]["user_id"]
            organization_id = operator_registration.json()["session"][
                "active_organization_id"
            ]
            operator_web = operator.cookies.get("vibecanvas-web-session")
            approver_web = approver.cookies.get("vibecanvas-web-session")
            assert operator_web and approver_web

            scope_id = (
                await operator.get("/api/v1/chats/bootstrap")
            ).json()["carrier_scope_id"]
            initial = await operator.post(
                f"/api/v1/chat-scopes/{scope_id}/chats/support_broker/messages",
                headers=_cookie_csrf(operator),
                json={"role": "user", "content": "initial"},
            )
            assert initial.status_code == 200, initial.text
            assert len(dispatched_turns) == 1

            await _grant_webauthn(operator_web)
            await _grant_webauthn(approver_web)
            monkeypatch.setattr(config, "privileged_access_enabled", True)
            monkeypatch.setattr(
                config,
                "privileged_support_operator_ids",
                frozenset({operator_id, approver_id}),
            )
            await _seed_operator_eligibilities(operator_id, approver_id)
            created = await operator.post(
                f"/api/v1/auth/privileged-access/organizations/"
                f"{organization_id}/requests",
                headers=_cookie_csrf(operator),
                json={
                    "resource_type": "chat",
                    "resource_id": "support_broker",
                    "actions": ["execute"],
                    "duration_seconds": 300,
                    "justification": "Customer-approved scoped runtime diagnosis",
                    "ticket_reference": "SUPPORT-MODEL-1",
                    "sensitive_scope_confirmed": True,
                },
            )
            assert created.status_code == 201, created.text
            access_request_id = created.json()["request_id"]
            approved = await approver.post(
                f"/api/v1/auth/privileged-access/organizations/"
                f"{organization_id}/requests/{access_request_id}/approve",
                headers=_cookie_csrf(approver),
                json={"sensitive_scope_confirmed": True},
            )
            assert approved.status_code == 200, approved.text
            activated = await operator.post(
                f"/api/v1/auth/privileged-access/organizations/"
                f"{organization_id}/requests/{access_request_id}/activate",
                headers=_cookie_csrf(operator),
            )
            assert activated.status_code == 200, activated.text

            supported_turn = await operator.post(
                f"/api/v1/chat-scopes/{scope_id}/chats/support_broker/messages",
                headers=_cookie_csrf(operator, "support"),
                json={"role": "user", "content": "diagnose"},
            )
            assert supported_turn.status_code == 200, supported_turn.text
            assert len(dispatched_turns) == 2
            turn = dispatched_turns[-1]
            capability = turn.model["api_key"]
            verified = verify_runtime_model_capability(
                capability,
                secret=config.signing_secret,
            )
            assert verified is not None
            assert verified.membership_id == f"privileged:{access_request_id}"

            async with pg_engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT set_config('app.tenant_id', :tenant_id, false)"
                    ),
                    {"tenant_id": organization_id},
                )
                await connection.execute(
                    text(
                        "UPDATE agent_runs SET status='running' "
                        "WHERE run_id=:run_id"
                    ),
                    {"run_id": turn.turn_id},
                )
            brokered = await operator.post(
                "/api/internal/runtime-model/v1/chat/completions?stream=true",
                headers={"Authorization": f"Bearer {capability}"},
                json={"model": "gpt-support-broker-test", "messages": []},
            )
            assert brokered.status_code == 200, brokered.text
            assert brokered.json()["id"] == "privileged-host-brokered"
            assert len(upstream_calls) == 1
            assert upstream_calls[0]["headers"]["authorization"] == (
                "Bearer support-provider-secret"
            )

            revoked = await approver.post(
                f"/api/v1/auth/privileged-access/organizations/"
                f"{organization_id}/requests/{access_request_id}/revoke",
                headers=_cookie_csrf(approver),
            )
            assert revoked.status_code == 200, revoked.text
            denied = await operator.post(
                "/api/internal/runtime-model/v1/chat/completions",
                headers={"Authorization": f"Bearer {capability}"},
                json={"model": "gpt-support-broker-test", "messages": []},
            )
            assert denied.status_code == 403
            assert denied.json()["detail"]["code"] == (
                "runtime_model_session_revoked"
            )
            assert len(upstream_calls) == 1
    finally:
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
async def test_workflow_runtime_broker_keeps_saved_secret_host_only_and_revokes_membership(
    client,
    pg_engine,
    monkeypatch,
):
    from vibecanvas_api.routes import runtime_model_broker as broker_route
    from vibecanvas_api.services.llm_credentials_inject import (
        build_llm_credentials_extra,
    )
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.execution_repo import ExecutionRepo

    upstream_request: dict[str, object] = {}

    async def upstream_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        header_blob = await reader.readuntil(b"\r\n\r\n")
        head, initial = header_blob.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            name, value = line.split(":", 1)
            headers[name.casefold()] = value.strip()
        content_length = int(headers.get("content-length", "0"))
        body = bytearray(initial)
        if len(body) < content_length:
            body.extend(await reader.readexactly(content_length - len(body)))
        upstream_request.update(
            request_line=lines[0],
            headers=headers,
            body=bytes(body),
        )
        payload = b'{"id":"workflow-host-brokered","choices":[]}'
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + payload
        )
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
    port = upstream.sockets[0].getsockname()[1]

    async def validate_test_destination(value: str, *, label: str):
        assert value == "https://provider.example/v1"
        assert label == "model API URL"
        return (
            f"http://127.0.0.1:{port}/v1",
            "127.0.0.1",
            ("127.0.0.1",),
        )

    monkeypatch.setattr(
        broker_route,
        "_validated_user_destination",
        validate_test_destination,
    )

    email = f"workflow_broker_{uuid.uuid4().hex[:12]}@example.com"
    register = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "username": "Workflow Broker User",
            "password": "pw12345678",
        },
    )
    assert register.status_code in (200, 201), register.text
    headers = {"Authorization": f"Bearer {register.json()['session_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workflow_response = await client.post(
        "/api/v1/workflows",
        json={"name": "Brokered workflow"},
        headers=headers,
    )
    assert workflow_response.status_code == 201, workflow_response.text
    workflow_id = workflow_response.json()["wf_id"]
    provider_secret = "workflow-provider-secret-on-host"
    credential_response = await client.post(
        "/api/v1/llm-credentials",
        json={
            "name": "Workflow Provider",
            "description": "broker integration",
            "provider": "openai",
            "model_name": "gpt-workflow-broker-test",
            "model_context_tokens": 128000,
            "api_url": "https://provider.example/v1",
            "api_key": provider_secret,
        },
        headers=headers,
    )
    assert credential_response.status_code == 201, credential_response.text
    credential_id = credential_response.json()["id"]
    execution_id = f"execution-{uuid.uuid4()}"
    workflow = {
        "__meta__": {"workflow_id": workflow_id},
        "node_1": {
            "node_id": "node_1",
            "node_name": "prompt",
            "node_type": "PromptNode",
            "node_config": {
                "model_name": "Workflow Provider",
                "prompt_template": "hello",
            },
        },
    }
    async with session_scope(tenant_id=me["tenant_id"]) as session:
        await ExecutionRepo(session, me["user_id"]).start_execution(
            workflow_id,
            (1, 0),
            execution_id,
        )
        mapping = await build_llm_credentials_extra(
            workflow,
            session,
            organization_id=me["tenant_id"],
            user_id=me["user_id"],
            workflow_id=workflow_id,
            execution_id=execution_id,
            execution_resource_type="workflow_execution",
        )
    entry = mapping["Workflow Provider"]
    capability = entry["api_key"]
    assert capability != provider_secret
    assert provider_secret not in repr(mapping)
    assert "provider.example" not in repr(mapping)
    assert credential_id not in entry["api_url"]

    try:
        response = await client.post(
            "/api/internal/runtime-model/v1/chat/completions?stream=true",
            headers={
                "Authorization": f"Bearer {capability}",
                "Content-Type": "application/json",
                "Cookie": "must-not-forward=1",
            },
            json={
                "model": "gpt-workflow-broker-test",
                "messages": [],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["id"] == "workflow-host-brokered"
        assert upstream_request["request_line"] == (
            "POST /v1/chat/completions?stream=true HTTP/1.1"
        )
        upstream_headers = upstream_request["headers"]
        assert isinstance(upstream_headers, dict)
        assert upstream_headers["authorization"] == f"Bearer {provider_secret}"
        assert "cookie" not in upstream_headers
        assert capability not in repr(upstream_request)
    finally:
        upstream.close()
        await upstream.wait_closed()

    async with pg_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE workflow_run_state SET status='stopped' "
                "WHERE wf_id=:workflow_id"
            ),
            {"workflow_id": workflow_id},
        )
    inactive = await client.post(
        "/api/internal/runtime-model/v1/chat/completions",
        headers={"Authorization": f"Bearer {capability}"},
        json={"model": "gpt-workflow-broker-test", "messages": []},
    )
    assert inactive.status_code == 403
    assert inactive.json()["detail"]["code"] == "runtime_model_execution_inactive"

    async with pg_engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE workflow_run_state SET status='running' "
                "WHERE wf_id=:workflow_id"
            ),
            {"workflow_id": workflow_id},
        )
        await connection.execute(
            text(
                "UPDATE org_memberships SET status='suspended' "
                "WHERE user_id=:user_id AND tenant_id=:tenant_id"
            ),
            {
                "user_id": me["user_id"],
                "tenant_id": me["tenant_id"],
            },
        )
    denied = await client.post(
        "/api/internal/runtime-model/v1/chat/completions",
        headers={"Authorization": f"Bearer {capability}"},
        json={"model": "gpt-workflow-broker-test", "messages": []},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "runtime_model_membership_revoked"
