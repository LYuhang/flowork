"""The active Runtime receives the host-authorized Platform MCP policy."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.authorization.openfga_client import (
    OpenFgaReadPage,
    OpenFgaTuple,
)
from vibecanvas_api.config import config
from vibecanvas_api.services.agent_runtime.protocol import (
    RuntimeCapabilities,
    RuntimeModelOption,
    RuntimeReasoningEffortOption,
    RuntimeType,
)
from vibecanvas_api.services.agent_runtime.model_capability import (
    verify_runtime_model_capability,
)
from vibecanvas_api.services.platform_mcp.authorization import (
    prepare_platform_tool,
)
from vibecanvas_api.services.platform_mcp.capability import (
    verify_platform_mcp_capability,
)
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


class _RelationshipStore:
    """Minimal OpenFGA wire-contract fake for this cross-runtime test."""

    def __init__(self) -> None:
        self.tuples: set[OpenFgaTuple] = set()

    async def read(self, *, tuple_key, **_kwargs):
        return OpenFgaReadPage(
            (tuple_key,) if tuple_key in self.tuples else (),
        )

    async def write(self, *, writes=(), deletes=()):
        self.tuples.update(writes)
        self.tuples.difference_update(deletes)

    async def batch_check(self, checks, **_kwargs):
        return tuple(
            self._allowed(user, relation, object_)
            for user, relation, object_ in checks
        )

    async def list_objects(
        self,
        *,
        user,
        relation,
        object_type,
        **_kwargs,
    ):
        return tuple(
            item.object.split(":", 1)[1]
            for item in sorted(self.tuples, key=lambda item: item.object)
            if item.object.startswith(f"{object_type}:")
            and self._allowed(user, relation, item.object)
        )

    def _has(self, user: str, relation: str, object_: str) -> bool:
        return OpenFgaTuple(user, relation, object_) in self.tuples

    def _allowed(self, user: str, relation: str, object_: str) -> bool:
        if object_.startswith("organization:"):
            roles = {"owner", "admin", "member"}
            return relation == "can_create_resource" and any(
                self._has(user, role, object_) for role in roles
            )
        role_map = {
            "can_view_metadata": {
                "creator", "viewer", "editor", "operator", "manager",
            },
            "can_view": {"creator", "viewer", "editor", "operator", "manager"},
            "can_update": {"creator", "editor", "manager"},
            "can_use": {"creator", "viewer", "editor", "operator", "manager"},
            "can_execute": {"creator", "operator", "manager"},
            "can_mount": {"creator", "viewer", "editor", "operator", "manager"},
        }
        return any(
            self._has(user, role, object_)
            for role in role_map.get(relation, set())
        )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(client, label: str) -> tuple[dict[str, str], dict]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{label}_{uuid.uuid4().hex[:12]}@example.com",
            "username": label,
            "password": "pw12345678",
        },
    )
    assert response.status_code == 201, response.text
    headers = _headers(response.json()["session_token"])
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    return headers, me


@pytest.mark.asyncio
async def test_codex_platform_mcp_enforces_allow_deny_boundary(
    client,
    pg_engine,
    monkeypatch,
) -> None:
    from vibecanvas_api.routes import chats as chats_route
    from vibecanvas_api.services.platform_mcp import invocation as platform_invocation

    store = _RelationshipStore()
    client._transport.app.state.openfga_client = store
    monkeypatch.setattr(platform_invocation, "_OPENFGA_CLIENT", store)

    dispatched = []

    class FakeRuntimeOrchestrator:
        async def stream_turn(self, **kwargs):
            dispatched.append(kwargs["turn_request"])
            yield ("NO_OP", {})

    async def fake_codex_capabilities(_credential_rows, **_kwargs):
        model = RuntimeModelOption(
            id="codex:account:gpt-security-test",
            label="Codex security test",
            is_default=True,
        )
        return RuntimeCapabilities(
            runtime_type=RuntimeType.CODEX,
            runtime_available=True,
            authenticated=True,
            source="test",
            models=[model],
            default_model_id=model.id,
        )

    monkeypatch.setattr(
        chats_route,
        "AgentRuntimeOrchestrator",
        FakeRuntimeOrchestrator,
    )
    monkeypatch.setattr(
        chats_route,
        "codex_capabilities",
        fake_codex_capabilities,
    )

    owner_headers, owner = await _register(client, "runtime_parity_owner")
    _outsider_headers, outsider = await _register(
        client,
        "runtime_parity_outsider",
    )
    workflow = await client.post(
        "/api/v1/workflows",
        headers=owner_headers,
        json={"name": "Runtime parity allowed"},
    )
    assert workflow.status_code == 201, workflow.text
    allowed_workflow_id = workflow.json()["wf_id"]
    async with session_scope(tenant_id=owner["tenant_id"]) as session:
        denied_workflow = await WorkflowRepo(
            session,
            outsider["user_id"],
        ).create_workflow(name="Runtime parity denied")
    denied_workflow_id = str(denied_workflow["wf_id"])

    bootstrap = await client.get(
        "/api/v1/chats/bootstrap",
        headers=owner_headers,
    )
    assert bootstrap.status_code == 200, bootstrap.text
    carrier_scope_id = bootstrap.json()["carrier_scope_id"]

    sent = await client.post(
        f"/api/v1/chat-scopes/{carrier_scope_id}/chats/chat-platform-codex/messages",
        headers=owner_headers,
        json={"role": "user", "content": "/workflow inspect access"},
    )
    assert sent.status_code == 200, sent.text

    assert [request.runtime_type.value for request in dispatched] == ["codex"]
    capabilities = []
    for request in dispatched:
        descriptor = next(
            item
            for item in request.mcp_host_servers
            if item.source == "platform" and item.name == "build"
        )
        capability = verify_platform_mcp_capability(
            descriptor.connection["capability"],
            secret=config.signing_secret,
            server="build",
        )
        assert capability is not None
        assert capability.runtime_session_id == request.runtime_session_id
        capabilities.append(capability)

    async with pg_engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, false)"),
            {"tenant_id": owner["tenant_id"]},
        )
        for capability in capabilities:
            await connection.execute(
                text(
                    "UPDATE agent_runs SET status='running' "
                    "WHERE run_id=:run_id"
                ),
                {"run_id": capability.turn_id},
            )

    results: list[tuple[bool, bool]] = []
    for capability in capabilities:
        context = await platform_invocation._context_for(capability)
        await prepare_platform_tool(
            context,
            server="build",
            tool_name="set_workflow",
            arguments={"workflow_id": allowed_workflow_id},
        )
        denied = False
        try:
            await prepare_platform_tool(
                context,
                server="build",
                tool_name="set_workflow",
                arguments={"workflow_id": denied_workflow_id},
            )
        except ToolError as exc:
            assert "permission_denied" in str(exc)
            denied = True
        results.append((True, denied))

    assert results == [(True, True)]


@pytest.mark.asyncio
async def test_codex_personal_api_uses_the_host_model_broker(
    client,
    monkeypatch,
) -> None:
    """A catalogued personal API must not fall into Codex's no-source guard."""
    from vibecanvas_api.routes import chats as chats_route

    dispatched = []

    class FakeRuntimeOrchestrator:
        async def stream_turn(self, **kwargs):
            dispatched.append(kwargs["turn_request"])
            yield ("NO_OP", {})

    headers, me = await _register(client, "codex_personal_api")
    credential = await client.post(
        "/api/v1/llm-credentials",
        headers=headers,
        json={
            "name": "Codex personal API",
            "description": "route regression",
            "provider": "openai",
            "model_name": "gpt-codex-personal-test",
            "model_context_tokens": 128000,
            "api_url": "https://provider.example/v1",
            "api_key": "personal-provider-secret",
        },
    )
    assert credential.status_code == 201, credential.text
    credential_id = credential.json()["id"]
    public_model_id = f"codex:credential:{credential_id}"

    async def fake_codex_capabilities(_credential_rows, **_kwargs):
        model = RuntimeModelOption(
            id=public_model_id,
            label="Codex personal API",
            description="Versioned personal provider model",
            provider="openai",
            context_length=128000,
            input_modalities=["text", "image"],
            supports_tools=True,
            supported_reasoning_efforts=[
                RuntimeReasoningEffortOption(
                    id="high",
                    label="High",
                    description="Deeper reasoning",
                ),
            ],
            default_reasoning_effort="high",
            is_default=True,
        )
        return RuntimeCapabilities(
            runtime_type=RuntimeType.CODEX,
            runtime_available=True,
            authenticated=True,
            source="test",
            models=[model],
            default_model_id=model.id,
        )

    monkeypatch.setattr(
        chats_route,
        "AgentRuntimeOrchestrator",
        FakeRuntimeOrchestrator,
    )
    monkeypatch.setattr(
        chats_route,
        "codex_capabilities",
        fake_codex_capabilities,
    )
    settings = await client.put(
        "/api/v1/agent-runtime/settings",
        headers=headers,
        json={"default_runtime_type": "codex"},
    )
    assert settings.status_code == 200, settings.text
    scope_id = (
        await client.get("/api/v1/chats/bootstrap", headers=headers)
    ).json()["carrier_scope_id"]

    sent = await client.post(
        f"/api/v1/chat-scopes/{scope_id}/chats/codex-personal/messages",
        headers=headers,
        json={
            "role": "user",
            "content": "hello",
            "agent_settings": {"model_id": public_model_id},
        },
    )
    assert sent.status_code == 200, sent.text
    assert len(dispatched) == 1
    model = dispatched[0].model
    assert model["id"] == "gpt-codex-personal-test"
    assert model["base_url"].endswith("/api/internal/runtime-model/v1")
    assert model["label"] == "Codex personal API"
    assert model["description"] == "Versioned personal provider model"
    assert model["context_length"] == 128000
    assert model["input_modalities"] == ["text", "image"]
    assert model["supports_tools"] is True
    assert model["supported_reasoning_efforts"] == [
        {
            "id": "high",
            "label": "High",
            "description": "Deeper reasoning",
        }
    ]
    assert model["default_reasoning_effort"] == "high"
    assert "personal-provider-secret" not in repr(model)
    capability = verify_runtime_model_capability(
        model["api_key"],
        secret=config.signing_secret,
    )
    assert capability is not None
    assert capability.organization_id == me["tenant_id"]
    assert capability.credential_id == credential_id
