from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from vibecanvas_api.services.agent_runtime.codex import (
    _CODEX_APPROVAL_SERVER_REQUESTS,
    _CODEX_INTERACTIVE_SERVER_REQUESTS,
    _CODEX_PROJECTED_ITEM_KINDS,
    _CODEX_PROJECTED_NOTIFICATIONS,
    _CODEX_RECOGNIZED_NOTIFICATIONS,
    _CODEX_REJECTED_SERVER_REQUESTS,
    _CODEX_SUPPRESSED_ITEM_KINDS,
    _CODEX_SUPPRESSED_NOTIFICATIONS,
    _ToolCompletionEvidence,
    _approval_policy,
    _approval_response,
    _broker_model_catalog,
    _broker_model_config,
    codex_app_server_startup_key,
    _codex_env,
    create_codex_app_server,
    _file_change_progress,
    _interaction_definition,
    _interaction_response,
    _interactive_artifact_from_item,
    _read_history_coverage,
    _McpItemCorrelator,
    _command_completion_reminder,
    _missing_command_completion_tools,
    _normalize_codex_plan,
    _RuntimeControlRouter,
    _safe_codex_notice,
    _tool_completion_status,
    _tool_projection,
    _write_history_coverage,
    run_codex_turn as _run_codex_turn,
)
from vibecanvas_api.services.agent_runtime.codex_app_server import (
    CodexAppServerError,
)
from vibecanvas_api.services.agent_runtime.protocol import RuntimeTurnRequest
from vibecanvas_api.services.agent_runtime.mcp_hub import SandboxMcpHub
from vibecanvas_api.services.agent_runtime.mcp_hub_adapter import (
    SandboxMcpRuntimeAdapter,
)
from vibecanvas_api.services.agent_runtime.mcp_runtime_protocol import (
    McpDesiredState,
    McpExecutionContext,
)
from vibecanvas_engine.sandbox_bus import MSG_RUNTIME_RESULT

_BROKER_MODEL = {
    "id": "gpt-codex-current",
    "base_url": "http://platform.test/api/internal/runtime-model/v1",
    "api_key": "turn-capability",
    "label": "Broker model",
    "description": "Dynamic provider model",
    "context_length": 1_000_000,
    "input_modalities": ["text"],
    "supports_tools": True,
    "supported_reasoning_efforts": [
        {"id": "low", "label": "low", "description": "Fast"},
        {"id": "high", "label": "high", "description": "Deep"},
    ],
    "default_reasoning_effort": "high",
}
_LOCKED_CODEX_VERSION = "codex-cli 0.147.0"
_LOCKED_CODEX_SCHEMA_SHA256 = (
    "babfd5c98cd978dd858b4762cdfbc9fba941e1a0e4053de0050e4082ae1f075a"
)
_RESIDENT_TEST_HUBS: dict[
    int,
    tuple[SandboxMcpRuntimeAdapter, SandboxMcpHub, dict[str, object]],
] = {}


def test_command_completion_gate_is_format_aware(tmp_path):
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "create a presentation"},
        model=_BROKER_MODEL,
        command_context={
            "active_modes": ["document"],
            "activated_this_turn": ["document"],
        },
        instructions=[{
            "instruction_id": "command:document:v1",
            "kind": "command_context",
            "scope": "chat",
            "name": "document",
            "version": 1,
            "content": "Create and review the document.",
            "activated_this_turn": True,
        }],
    )

    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"presentation-v1")
    deck_hash = hashlib.sha256(deck.read_bytes()).hexdigest()
    review = _ToolCompletionEvidence(
        tool_input={"path": str(deck)},
        path=str(deck),
        sha256=deck_hash,
    )
    assert _missing_command_completion_tools(request, {}) == (
        "review_document",
        "render_interactive",
    )
    assert _missing_command_completion_tools(
        request,
        {"review_document": [review]},
    ) == ("render_document_feedback", "render_interactive")
    brief = tmp_path / "brief.md"
    brief.write_text("Ready", encoding="utf-8")
    brief_hash = hashlib.sha256(brief.read_bytes()).hexdigest()
    brief_evidence = _ToolCompletionEvidence(
        tool_input={"path": str(brief)},
        path=str(brief),
        sha256=brief_hash,
    )
    assert _missing_command_completion_tools(
        request,
        {
            "review_document": [brief_evidence],
            "render_interactive": [brief_evidence],
        },
    ) == ()


def test_command_completion_gate_invalidates_stale_file_evidence(tmp_path):
    path = tmp_path / "report.xlsx"
    path.write_bytes(b"accepted-v1")
    accepted_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    item = _ToolCompletionEvidence(
        tool_input={"path": str(path)},
        path=str(path),
        sha256=accepted_hash,
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "create a workbook"},
        model=_BROKER_MODEL,
        command_context={
            "active_modes": ["document"],
            "activated_this_turn": ["document"],
        },
        instructions=[{
            "instruction_id": "command:document:v1",
            "kind": "command_context",
            "scope": "chat",
            "name": "document",
            "version": 1,
            "content": "Create and review the document.",
            "activated_this_turn": True,
        }],
    )
    evidence = {
        "review_document": [item],
        "render_document_feedback": [item],
        "render_interactive": [item],
    }
    assert _missing_command_completion_tools(request, evidence) == ()

    path.write_bytes(b"modified-after-preview")

    assert _missing_command_completion_tools(request, evidence) == (
        "review_document",
        "render_document_feedback",
        "render_interactive",
    )


def test_command_completion_gate_requires_validated_workflow_publication():
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "create a workflow"},
        model=_BROKER_MODEL,
        command_context={
            "active_modes": ["workflow"],
            "activated_this_turn": ["workflow"],
        },
        instructions=[{
            "instruction_id": "command:workflow:v1",
            "kind": "command_context",
            "scope": "chat",
            "name": "workflow",
            "version": 1,
            "content": "Build, validate, and publish the workflow.",
            "activated_this_turn": True,
        }],
    )
    checked = _ToolCompletionEvidence(
        tool_input={"workflow_path": "/data/workflow.json"},
        path="/data/workflow.json",
        sha256=None,
    )
    published = _ToolCompletionEvidence(
        tool_input={"workflow_path": "/data/workflow.json"},
        path="/data/workflow.json",
        sha256=None,
    )

    assert _missing_command_completion_tools(request, {}) == (
        "check_workflow",
        "update_canvas",
    )
    assert _missing_command_completion_tools(
        request,
        {"check_workflow": [checked]},
    ) == ("update_canvas",)
    assert _missing_command_completion_tools(
        request,
        {"check_workflow": [checked], "update_canvas": [published]},
    ) == ()

    wrong_file = _ToolCompletionEvidence(
        tool_input={"workflow_path": "/data/other.json"},
        path="/data/other.json",
        sha256=None,
    )
    assert _missing_command_completion_tools(
        request,
        {"check_workflow": [checked], "update_canvas": [wrong_file]},
    ) == ("check_workflow",)

    bypassed_validation = _ToolCompletionEvidence(
        tool_input={
            "workflow_path": "/data/workflow.json",
            "require_valid": False,
        },
        path="/data/workflow.json",
        sha256=None,
    )
    assert _missing_command_completion_tools(
        request,
        {
            "check_workflow": [checked],
            "update_canvas": [bypassed_validation],
        },
    ) == ("update_canvas",)


def test_command_completion_reminder_identifies_reviewed_publication_path():
    reminder = _command_completion_reminder(
        ("render_interactive",),
        {
            "review_document": [
                _ToolCompletionEvidence(
                    tool_input={"path": "/data/final/report.xlsx"},
                    path="/data/final/report.xlsx",
                    sha256="digest",
                )
            ]
        },
    )

    assert "path=\"/data/final/report.xlsx\"" in reminder
    assert "do not merely describe the call" in reminder
    assert "your very next action must be that `render_interactive` tool call" in reminder
    assert "Do not inspect, edit, review, render feedback" in reminder


@pytest.fixture(autouse=True)
def _isolate_broker_capability_file(monkeypatch):
    # Direct adapter tests inject a fake app-server client and must not depend
    # on a host Codex installation. Individual executable-discovery tests can
    # still override this fixture explicitly in their own body.
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._codex_executable",
        lambda: "/opt/test/codex",
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._install_broker_capability",
        lambda _capability: None,
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._remove_broker_capability",
        lambda: None,
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._install_broker_model_catalog",
        lambda _request, _executable: (
            "/tmp/vibecanvas-runtime/model-catalog-test.json"
        ),
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._write_history_coverage",
        lambda *_args, **_kwargs: None,
    )


async def run_codex_turn(channel, request, **kwargs):
    """Run direct adapter tests through the mandatory aggregate Hub."""
    if request.mcp_desired_state is not None:
        return await _run_codex_turn(channel, request, **kwargs)
    now = datetime.now(timezone.utc)
    desired = McpDesiredState(
        organization_id=request.tenant_id,
        user_id=request.user_id,
        chat_id=request.chat_id,
        runtime_session_id=request.runtime_session_id,
        sandbox_id="test-sandbox",
        sandbox_generation=1,
        chat_mcp_config_revision=request.mcp_config_revision,
        platform_contract_revision="test-platform",
        skill_catalog_revision="test-skills",
        servers=[],
    )
    execution = McpExecutionContext(
        organization_id=request.tenant_id,
        user_id=request.user_id,
        chat_id=request.chat_id,
        runtime_session_id=request.runtime_session_id,
        sandbox_generation=1,
        turn_id=request.turn_id,
        agent_run_id=request.turn_id,
        active_platform_capabilities=[],
        selected_mcp_revision=request.mcp_config_revision,
        approval_mode=request.approval_mode,
        surface=request.surface,
        authorization_generation="test-authorization",
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        capability="test-capability",
    )

    async def unused_gateway(*_args, **_kwargs):
        raise AssertionError("empty test Hub must not call the Host Gateway")

    resident_threads = kwargs.get("resident_threads")
    cache_key = id(resident_threads) if resident_threads is not None else None
    bundle = _RESIDENT_TEST_HUBS.get(cache_key) if cache_key is not None else None
    if bundle is None:
        adapter = SandboxMcpRuntimeAdapter(unused_gateway)
        hub = SandboxMcpHub(adapter)
        registry: dict[str, object] = {}
        if cache_key is not None:
            _RESIDENT_TEST_HUBS[cache_key] = (adapter, hub, registry)
    else:
        adapter, hub, registry = bundle
    try:
        return await _run_codex_turn(
            channel,
            request.model_copy(update={
                "mcp_runtime_stage": "sandbox",
                "mcp_host_servers": [],
                "mcp_desired_state": desired,
                "mcp_execution_context": execution,
            }),
            mcp_hub=hub,
            mcp_adapter=adapter,
            hub_gateway_registry=registry,
            **kwargs,
        )
    finally:
        keep_resident = cache_key is not None and request.runtime_state_ref is None
        if keep_resident:
            return
        if cache_key is not None:
            _RESIDENT_TEST_HUBS.pop(cache_key, None)
        gateway = registry.get("aggregate")
        if gateway is not None:
            await gateway.close()
        await hub.close()


def test_codex_approval_modes_map_to_native_policy() -> None:
    assert _approval_policy("agent") == "on-request"
    assert _approval_policy("always_ask") == "untrusted"
    assert _approval_policy("always_allow") == "never"


def test_codex_env_bypasses_only_the_runtime_loopback_gateway(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:18080")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:18080")
    monkeypatch.setenv("NO_PROXY", "localhost,internal.example.test")

    env = _codex_env(str(tmp_path))

    assert env["HTTP_PROXY"] == "http://127.0.0.1:18080"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:18080"
    assert env["NO_PROXY"] == "127.0.0.1"
    assert "internal.example.test" not in env["NO_PROXY"]


def test_codex_builds_method_specific_native_approval_responses() -> None:
    assert _approval_response(
        "item/commandExecution/requestApproval", {}, "approve"
    ) == {"decision": "accept"}
    assert _approval_response(
        "item/fileChange/requestApproval", {}, "deny"
    ) == {"decision": "decline"}
    requested = {"network": {"enabled": True}, "fileSystem": {"read": ["/data"]}}
    assert _approval_response(
        "item/permissions/requestApproval",
        {"permissions": requested},
        "approve",
    ) == {"permissions": requested, "scope": "turn"}
    assert _approval_response(
        "item/permissions/requestApproval",
        {"permissions": requested},
        "deny",
    ) == {"permissions": {}, "scope": "turn"}


def test_codex_request_user_input_uses_portable_form_and_native_answers() -> None:
    params = {
        "questions": [
            {
                "id": "scope",
                "header": "Scope",
                "question": "Which scope should I use?",
                "options": [
                    {"label": "Current file", "description": "Keep it focused"},
                    {"label": "Workspace", "description": "Scan everything"},
                ],
            },
            {
                "id": "token",
                "header": "Credential",
                "question": "Temporary token",
                "isSecret": True,
            },
        ]
    }
    definition = _interaction_definition(
        "item/tool/requestUserInput",
        params,
        artifact_id="ia_input",
        hitl_request_id="hitl_input",
    )

    assert definition["component_type"] == "user_input"
    assert definition["completion_mode"] == "wait_for_submit"
    assert definition["interaction_schema"]["hide_result"] is True
    assert definition["props"]["questions"][0]["options"][0]["value"] == (
        "Current file"
    )
    assert definition["props"]["questions"][1]["secret"] is True
    assert _interaction_response(
        "item/tool/requestUserInput",
        params,
        {
            "action": "submit",
            "payload": {
                "interaction_result": {
                    "widget_state": {
                        "scope": "Workspace",
                        "token": "private-value",
                    }
                }
            },
        },
    ) == {
        "answers": {
            "scope": {"answers": ["Workspace"]},
            "token": {"answers": ["private-value"]},
        }
    }
    assert _interaction_response(
        "item/tool/requestUserInput",
        params,
        {"action": "cancel", "payload": {}},
    ) == {
        "answers": {
            "scope": {"answers": []},
            "token": {"answers": []},
        }
    }


def test_codex_mcp_elicitation_coerces_form_values_and_bounds_external_url() -> None:
    params = {
        "serverName": "crm",
        "message": "Choose export settings",
        "mode": "form",
        "requestedSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "title": "Row limit"},
                "includeDrafts": {"type": "boolean", "title": "Drafts"},
            },
        },
    }
    definition = _interaction_definition(
        "mcpServer/elicitation/request",
        params,
        artifact_id="ia_mcp",
        hitl_request_id="hitl_mcp",
    )
    assert definition["title"] == "crm needs input"
    assert definition["props"]["questions"][1]["options"] == [
        {"label": "Yes", "value": "true"},
        {"label": "No", "value": "false"},
    ]
    assert _interaction_response(
        "mcpServer/elicitation/request",
        params,
        {
            "action": "submit",
            "payload": {
                "decision_payload": {
                    "widget_state": {"limit": "25", "includeDrafts": "false"}
                }
            },
        },
    ) == {"action": "accept", "content": {"limit": 25, "includeDrafts": False}}
    assert _interaction_response(
        "mcpServer/elicitation/request",
        params,
        {"action": "cancel", "payload": {}},
    ) == {"action": "cancel"}

    unsafe = _interaction_definition(
        "mcpServer/elicitation/request",
        {"serverName": "crm", "mode": "url", "url": "javascript:alert(1)"},
        artifact_id="ia_url",
        hitl_request_id="hitl_url",
    )
    assert "url" not in unsafe["props"]


def test_codex_native_progress_and_notices_are_bounded_and_sanitized() -> None:
    progress = json.loads(_file_change_progress({
        "changes": [
            {"path": "/data/project/app.py", "kind": "update", "diff": "+safe"},
            {"path": "/host/private/secret.py", "kind": "add", "diff": "+value"},
        ]
    }))
    assert progress == {
        "changes": [
            {"path": "project/app.py", "kind": "update", "diff": "+safe"},
            {"path": "secret.py", "kind": "add", "diff": "+value"},
        ],
        "truncated": False,
    }
    retry = _safe_codex_notice(
        "error",
        {"error": {"message": "Temporary provider failure"}, "willRetry": True},
    )
    assert retry == {
        "level": "warning",
        "code": "codex_runtime_retry",
        "message": "Temporary provider failure",
        "runtime_type": "codex",
        "native_kind": "error",
        "retrying": True,
        "turn_disposition": "continue",
    }
    reroute = _safe_codex_notice(
        "model/rerouted",
        {"fromModel": "gpt-a", "toModel": "gpt-b", "reason": "policy"},
    )
    assert reroute is not None
    assert reroute["code"] == "codex_model_rerouted"


def test_codex_model_provider_uses_volatile_command_auth_without_token_in_config() -> None:
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="runtime-session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "hello"},
        model=_BROKER_MODEL,
    )

    capability, config = _broker_model_config(request)

    assert capability == "turn-capability"
    assert capability not in repr(config)
    assert config["model_catalog_json"].startswith(
        "/tmp/vibecanvas-runtime/model-catalog-"
    )
    assert config["model_context_window"] == 1_000_000
    provider = config["model_providers"]["vibecanvas_runtime_model"]
    assert provider["base_url"].endswith("/api/internal/runtime-model/v1")
    assert provider["namespace_tools"] is False
    assert provider["auth"] == {
        "command": "/bin/cat",
        "args": ["/tmp/vibecanvas-runtime/model-capability"],
        "timeout_ms": 1_000,
        "refresh_interval_ms": 1,
    }


def test_codex_openrouter_model_disables_unadvertised_hosted_search() -> None:
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="runtime-session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "hello"},
        model={
            **_BROKER_MODEL,
            "provider": "openrouter",
            "api_source": "openrouter_oauth",
            "supports_web_search": False,
        },
    )

    _, config = _broker_model_config(request)

    assert config["web_search"] == "disabled"


def test_codex_openrouter_model_enables_advertised_hosted_search() -> None:
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="runtime-session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "hello"},
        model={
            **_BROKER_MODEL,
            "provider": "openrouter",
            "api_source": "openrouter_oauth",
            "supports_web_search": True,
        },
    )

    _, config = _broker_model_config(request)

    assert config["web_search"] == "live"


def test_codex_dynamic_catalog_uses_provider_metadata_and_versioned_prompt(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._bundled_model_template",
        lambda _executable: {
            "base_instructions": "Version-matched Codex instructions",
            "shell_type": "shell_command",
            "truncation_policy": {"mode": "tokens", "limit": 10_000},
            "context_window": 200_000,
        },
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="runtime-session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "hello"},
        model={**_BROKER_MODEL, "id": "stealth/ox-alpha"},
    )

    catalog = _broker_model_catalog(request, "/opt/codex/bin/codex")

    assert len(catalog["models"]) == 1
    model = catalog["models"][0]
    assert model["slug"] == "stealth/ox-alpha"
    assert model["display_name"] == "Broker model"
    assert model["context_window"] == 1_000_000
    assert model["input_modalities"] == ["text"]
    assert model["default_reasoning_level"] == "high"
    assert [item["effort"] for item in model["supported_reasoning_levels"]] == [
        "low",
        "high",
    ]
    assert model["base_instructions"] == "Version-matched Codex instructions"
    assert model["support_verbosity"] is False
    assert _BROKER_MODEL["api_key"] not in json.dumps(catalog)


def test_codex_app_server_loads_dynamic_catalog_at_process_start(monkeypatch) -> None:
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._codex_executable",
        lambda: "/opt/codex/bin/codex",
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._codex_env",
        lambda _runtime_root: {"CODEX_HOME": "/runtime/.codex"},
    )
    broker = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="turn",
        runtime_type="codex",
        runtime_session_id="runtime-session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "hello"},
        model={**_BROKER_MODEL, "id": "stealth/ox-alpha"},
    )
    account = broker.model_copy(update={
        "model": {
            "id": "gpt-5.6-sol",
            "connection_type": "chatgpt_account",
        },
    })

    client = create_codex_app_server(broker)

    assert client._config_overrides == (
        f'model_catalog_json="{_broker_model_config(broker)[1]["model_catalog_json"]}"',
    )
    assert codex_app_server_startup_key(broker).startswith("broker:")
    assert create_codex_app_server(account)._config_overrides == ()
    assert codex_app_server_startup_key(account) == "chatgpt-account"


def test_codex_extracts_interactive_artifact_from_mcp_structured_content() -> None:
    envelope = {
        "status": "success",
        "payload": {
            "kind": "interactive_artifact",
            "artifact": {
                "kind": "interactive_artifact",
                "artifact_id": "ia_codex",
                "completion_mode": "wait_for_submit",
            },
        },
        "meta": {"tool": "render_interactive"},
    }
    item = {
        "type": "mcpToolCall",
        "result": {"structuredContent": envelope},
    }
    assert _interactive_artifact_from_item(item) == envelope


def test_codex_plan_snapshot_maps_to_frontend_todo_contract() -> None:
    assert _normalize_codex_plan([
        {"step": "Inspect files", "status": "completed"},
        {"step": "Implement change", "status": "inProgress"},
        {"step": "Run tests", "status": "pending"},
    ]) == [
        {"id": 1, "text": "Inspect files", "status": "done"},
        {"id": 2, "text": "Implement change", "status": "in_progress"},
        {"id": 3, "text": "Run tests", "status": "pending"},
    ]


def test_codex_0147_notification_manifest_has_no_unclassified_method() -> None:
    schema_methods = {
        "account/login/completed",
        "account/rateLimits/updated",
        "account/updated",
        "app/list/updated",
        "command/exec/outputDelta",
        "configWarning",
        "deprecationNotice",
        "error",
        "externalAgentConfig/import/completed",
        "externalAgentConfig/import/progress",
        "fs/changed",
        "fuzzyFileSearch/sessionCompleted",
        "fuzzyFileSearch/sessionUpdated",
        "guardianWarning",
        "hook/completed",
        "hook/started",
        "item/agentMessage/delta",
        "item/autoApprovalReview/completed",
        "item/autoApprovalReview/started",
        "item/commandExecution/outputDelta",
        "item/commandExecution/terminalInteraction",
        "item/completed",
        "item/fileChange/outputDelta",
        "item/fileChange/patchUpdated",
        "item/mcpToolCall/progress",
        "item/plan/delta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/textDelta",
        "item/started",
        "mcpServer/oauthLogin/completed",
        "mcpServer/startupStatus/updated",
        "model/rerouted",
        "model/safetyBuffering/updated",
        "model/verification",
        "process/exited",
        "process/outputDelta",
        "remoteControl/status/changed",
        "serverRequest/resolved",
        "skills/changed",
        "thread/archived",
        "thread/closed",
        "thread/compacted",
        "thread/deleted",
        "thread/environment/connected",
        "thread/environment/disconnected",
        "thread/goal/cleared",
        "thread/goal/updated",
        "thread/name/updated",
        "thread/realtime/closed",
        "thread/realtime/error",
        "thread/realtime/itemAdded",
        "thread/realtime/outputAudio/delta",
        "thread/realtime/sdp",
        "thread/realtime/started",
        "thread/realtime/transcript/delta",
        "thread/realtime/transcript/done",
        "thread/settings/updated",
        "thread/started",
        "thread/status/changed",
        "thread/tokenUsage/updated",
        "thread/unarchived",
        "turn/completed",
        "turn/diff/updated",
        "turn/moderationMetadata",
        "turn/plan/updated",
        "turn/started",
        "warning",
        "windows/worldWritableWarning",
        "windowsSandbox/setupCompleted",
    }

    assert _CODEX_RECOGNIZED_NOTIFICATIONS == schema_methods
    assert _CODEX_PROJECTED_NOTIFICATIONS <= schema_methods
    assert _CODEX_SUPPRESSED_NOTIFICATIONS <= schema_methods


def test_codex_0147_thread_item_manifest_has_no_unclassified_type() -> None:
    schema_item_types = {
        "agentMessage",
        "collabAgentToolCall",
        "commandExecution",
        "contextCompaction",
        "dynamicToolCall",
        "enteredReviewMode",
        "exitedReviewMode",
        "fileChange",
        "hookPrompt",
        "imageGeneration",
        "imageView",
        "mcpToolCall",
        "plan",
        "reasoning",
        "sleep",
        "subAgentActivity",
        "userMessage",
        "webSearch",
    }

    assert (
        _CODEX_PROJECTED_ITEM_KINDS | _CODEX_SUPPRESSED_ITEM_KINDS
    ) == schema_item_types
    assert not (
        _CODEX_PROJECTED_ITEM_KINDS & _CODEX_SUPPRESSED_ITEM_KINDS
    )


def test_codex_0147_server_request_manifest_closes_every_request() -> None:
    schema_request_methods = {
        "account/chatgptAuthTokens/refresh",
        "applyPatchApproval",
        "attestation/generate",
        "currentTime/read",
        "execCommandApproval",
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "item/tool/call",
        "item/tool/requestUserInput",
        "mcpServer/elicitation/request",
    }
    classified = (
        _CODEX_APPROVAL_SERVER_REQUESTS
        | _CODEX_INTERACTIVE_SERVER_REQUESTS
        | _CODEX_REJECTED_SERVER_REQUESTS
    )

    assert classified == schema_request_methods
    assert not (
        _CODEX_APPROVAL_SERVER_REQUESTS
        & _CODEX_INTERACTIVE_SERVER_REQUESTS
    )


def test_installed_codex_app_server_schema_matches_locked_baseline(tmp_path) -> None:
    executable = shutil.which("codex")
    if executable is None:
        pytest.skip("Codex CLI is not installed in this test environment")
    version = subprocess.run(  # noqa: S603 - exact resolved local CLI
        [executable, "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    assert version == _LOCKED_CODEX_VERSION

    output = tmp_path / "codex-schema"
    subprocess.run(  # noqa: S603 - exact resolved local CLI
        [
            executable,
            "app-server",
            "generate-json-schema",
            "--experimental",
            "--out",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    bundle = output / "codex_app_server_protocol.schemas.json"
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == (
        _LOCKED_CODEX_SCHEMA_SHA256
    )


def test_codex_projects_native_image_view_as_observable_tool() -> None:
    name, arguments, output, artifact = _tool_projection({
        "id": "image-1",
        "type": "imageView",
        "path": "/memory/diagram-review-artifacts/review_0123456789abcdef.png",
    })

    assert name == "view_image"
    assert arguments == (
        '{"path": '
        '"/memory/diagram-review-artifacts/review_0123456789abcdef.png"}'
    )
    assert output == "Image opened"
    assert artifact is None


@pytest.mark.parametrize(
    ("item", "expected_name"),
    [
        ({"type": "webSearch", "query": "diagram layout", "results": []}, "web_search"),
        (
            {
                "type": "collabAgentToolCall",
                "tool": "spawnAgent",
                "prompt": "Review the diagram",
                "receiverThreadIds": ["thread-child"],
                "agentsStates": {"thread-child": {"status": "completed"}},
            },
            "collab_agent",
        ),
        (
            {
                "type": "subAgentActivity",
                "agentPath": "diagram-reviewer",
                "kind": "message",
            },
            "subagent_activity",
        ),
        (
            {
                "type": "imageGeneration",
                "status": "completed",
                "result": "generated",
                "savedPath": "/data/generated.png",
            },
            "generate_image",
        ),
        ({"type": "sleep", "durationMs": 250}, "wait"),
        ({"type": "enteredReviewMode", "review": "Reviewing changes"}, "review_mode"),
        ({"type": "exitedReviewMode", "review": "Review complete"}, "review_mode"),
        ({"type": "contextCompaction"}, "context_compaction"),
    ],
)
def test_codex_projects_native_public_items_through_portable_tools(
    item: dict,
    expected_name: str,
) -> None:
    name, arguments, output, artifact = _tool_projection(item)

    assert name == expected_name
    assert isinstance(json.loads(arguments), dict)
    assert isinstance(output, str)
    assert artifact is None


def test_codex_reasoning_projection_exposes_summary_but_never_private_content() -> None:
    name, arguments, output, artifact = _tool_projection({
        "type": "reasoning",
        "summary": ["Checked the current layout", "Found an overlap"],
        "content": ["private hidden chain of thought"],
    })

    assert name == "reasoning_summary"
    assert json.loads(arguments) == {"summary_parts": 2}
    assert output == "Checked the current layout\n\nFound an overlap"
    assert "private hidden chain of thought" not in f"{arguments}{output}"
    assert artifact is None


def test_codex_hook_projection_discloses_activity_without_injected_prompt() -> None:
    name, arguments, output, artifact = _tool_projection({
        "type": "hookPrompt",
        "fragments": [{"hookRunId": "hook-1", "text": "private system instruction"}],
    })

    assert name == "runtime_hook"
    assert json.loads(arguments) == {"fragment_count": 1}
    assert output == "Runtime hook context applied"
    assert "private system instruction" not in f"{arguments}{output}"
    assert artifact is None


def test_codex_hook_run_projection_omits_source_path_and_output_entries() -> None:
    name, arguments, output, artifact = _tool_projection({
        "type": "hookPrompt",
        "run": {
            "eventName": "postToolUse",
            "scope": "turn",
            "executionMode": "sync",
            "status": "completed",
            "statusMessage": "Validation complete",
            "sourcePath": "/host/private/hooks/validate.py",
            "entries": [{"text": "private hook output"}],
        },
    })

    assert name == "runtime_hook"
    assert json.loads(arguments) == {
        "event": "postToolUse",
        "scope": "turn",
        "mode": "sync",
    }
    assert output == "Validation complete"
    assert "/host/private" not in f"{arguments}{output}"
    assert "private hook output" not in f"{arguments}{output}"
    assert artifact is None


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"status": "completed"}, "done"),
        ({"status": "declined"}, "error"),
        ({"status": "completed", "success": False}, "error"),
        ({"status": "completed", "error": {"message": "failed"}}, "error"),
        ({}, "done"),
    ],
)
def test_codex_normalizes_native_tool_terminal_status(item: dict, expected: str) -> None:
    assert _tool_completion_status(item) == expected


@pytest.mark.asyncio
async def test_runtime_control_router_separates_native_and_platform_requests() -> None:
    router = _RuntimeControlRouter()
    native = asyncio.create_task(router.wait("codex_app_server", 7))
    platform = asyncio.create_task(router.wait("platform_mcp", 7))
    await asyncio.sleep(0)

    router.deliver({
        "action": "approve",
        "correlation": {
            "source": "platform_mcp",
            "runtime_request_id": 7,
        },
    })
    assert (await platform)["action"] == "approve"
    assert not native.done()

    router.deliver({
        "action": "deny",
        "correlation": {
            "source": "codex_app_server",
            "runtime_request_id": 7,
        },
    })
    assert (await native)["action"] == "deny"


@pytest.mark.asyncio
async def test_runtime_control_router_stop_cancels_every_pending_gate() -> None:
    router = _RuntimeControlRouter()
    native = asyncio.create_task(router.wait("codex_app_server", "native"))
    platform = asyncio.create_task(router.wait("platform_mcp", "browser"))
    await asyncio.sleep(0)

    router.cancel()

    assert (await native)["action"] == "cancel"
    assert (await platform)["action"] == "cancel"


@pytest.mark.asyncio
async def test_mcp_item_correlator_keeps_gateway_and_runtime_ids_separate() -> None:
    correlator = _McpItemCorrelator()
    arguments = {"tab_id": 7, "selector": "#submit"}
    waiter = asyncio.create_task(correlator.wait("browser_click", arguments))
    await asyncio.sleep(0)

    correlator.register(
        "browser_click",
        {"selector": "#submit", "tab_id": 7},
        "exec-runtime-item",
    )

    assert await waiter == "exec-runtime-item"


class _Channel:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._never = asyncio.Event()

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def recv(self):
        await self._never.wait()


@pytest.mark.asyncio
async def test_codex_resident_thread_suppresses_repeated_mcp_startup(monkeypatch):
    class FakeAppServer:
        def __init__(self):
            self.requests: list[tuple[str, dict]] = []
            self.turn_number = 0

        async def start(self):
            return None

        async def request(self, method, params, **_kwargs):
            self.requests.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "codex-thread", "turns": []}}
            if method == "turn/start":
                self.turn_number += 1
                return {"turn": {"id": f"codex-turn-{self.turn_number}"}}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "mcpServer/startupStatus/updated",
                "params": {"name": "config", "status": "ready"},
            }
            message_id = f"answer-{self.turn_number}"
            yield {
                "method": "item/started",
                "params": {"item": {"id": message_id, "type": "agentMessage"}},
            }
            yield {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": message_id,
                        "type": "agentMessage",
                        "text": "done",
                    }
                },
            }
            yield {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "id": f"codex-turn-{self.turn_number}",
                        "status": "completed",
                    }
                },
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )

    client = FakeAppServer()
    resident_threads: dict[str, str] = {}
    first_channel = _Channel()
    base_request = dict(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        runtime_type="codex",
        runtime_session_id="runtime-session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "hello"},
        model=_BROKER_MODEL,
    )
    await run_codex_turn(
        first_channel,
        RuntimeTurnRequest(turn_id="platform-turn-1", **base_request),
        client=client,
        close_client=False,
        resident_threads=resident_threads,
    )

    second_channel = _Channel()
    await run_codex_turn(
        second_channel,
        RuntimeTurnRequest(
            turn_id="platform-turn-2",
            runtime_state_ref="codex-thread",
            **base_request,
        ),
        client=client,
        close_client=False,
        resident_threads=resident_threads,
    )

    assert [method for method, _params in client.requests] == [
        "thread/start",
        "turn/start",
        "turn/start",
    ]
    assert any(
        message.get("event", {}).get("type") == "tool.start"
        for message in first_channel.sent
    )
    assert not any(
        message.get("event", {}).get("type") in {"tool.start", "tool.end"}
        for message in second_channel.sent
    )


@pytest.mark.asyncio
async def test_codex_retries_one_empty_provider_completion(monkeypatch):
    class FakeAppServer:
        def __init__(self):
            self.requests: list[tuple[str, dict]] = []
            self.turn_number = 0

        async def start(self):
            return None

        async def request(self, method, params, **_kwargs):
            self.requests.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "codex-thread", "turns": []}}
            if method == "turn/start":
                self.turn_number += 1
                return {"turn": {"id": f"codex-turn-{self.turn_number}"}}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "turn/completed",
                "params": {
                    "turn": {"id": "codex-turn-1", "status": "completed"}
                },
            }
            yield {
                "method": "item/started",
                "params": {"item": {"id": "answer-2", "type": "agentMessage"}},
            }
            yield {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "answer-2",
                        "type": "agentMessage",
                        "text": "Recovered answer",
                    }
                },
            }
            yield {
                "method": "turn/completed",
                "params": {
                    "turn": {"id": "codex-turn-2", "status": "completed"}
                },
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )

    client = FakeAppServer()
    channel = _Channel()
    await run_codex_turn(
        channel,
        RuntimeTurnRequest(
            tenant_id="tenant",
            user_id="user",
            chat_id="chat",
            turn_id="platform-turn",
            runtime_type="codex",
            runtime_session_id="runtime-session",
            runtime_root="/runtime/.codex",
            message={"role": "user", "content": "hello"},
            model=_BROKER_MODEL,
        ),
        client=client,
        close_client=False,
        resident_threads={},
    )

    assert [method for method, _params in client.requests] == [
        "thread/start",
        "turn/start",
        "turn/start",
    ]
    assert any(
        message.get("event", {}).get("payload", {}).get("payload", {}).get("code")
        == "codex_empty_completion_retry"
        for message in channel.sent
    )
    assert any(
        message.get("event", {}).get("payload", {}).get("delta")
        == "Recovered answer"
        for message in channel.sent
    )


@pytest.mark.asyncio
async def test_codex_publishes_reviewed_document_through_completion_hub(
    monkeypatch,
):
    gateway_calls: list[tuple[str, dict]] = []

    class FakeCallResult:
        isError = False

        def model_dump(self, **_kwargs):
            return {
                "structuredContent": {
                    "schema_version": 1,
                    "status": "success",
                    "payload": {
                        "kind": "interactive_artifact",
                        "artifact": {
                            "kind": "interactive_artifact",
                            "schema_version": 1,
                            "artifact_id": "ia_completion_preview",
                            "title": "deck.pptx",
                            "component_type": "file_preview",
                            "props": {"path": "/data/deck.pptx"},
                            "completion_mode": "render_only",
                            "require_human_confirm": False,
                        },
                    },
                    "meta": {"tool": "render_interactive"},
                },
            }

    class FakeGateway:
        def __init__(self, *_args):
            self.url = "http://127.0.0.1:12345/"

        async def activate(self, **_kwargs):
            return [{
                "name": "interactive",
                "tools": [{"name": "render_interactive"}],
            }]

        async def call_tool(self, name, arguments):
            gateway_calls.append((name, dict(arguments)))
            return FakeCallResult()

        def deactivate(self):
            return None

        async def close(self):
            return None

    class FakeAppServer:
        def __init__(self):
            self.requests: list[tuple[str, dict]] = []
            self.turn_number = 0

        async def start(self):
            return None

        async def request(self, method, params, **_kwargs):
            self.requests.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "codex-thread", "turns": []}}
            if method == "turn/start":
                self.turn_number += 1
                return {"turn": {"id": f"codex-turn-{self.turn_number}"}}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "item/started",
                "params": {"item": {"id": "answer-1", "type": "agentMessage"}},
            }
            yield {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "answer-1",
                        "type": "agentMessage",
                        "text": "The presentation is ready.",
                    }
                },
            }
            for item_id, tool in (
                ("review", "review_document"),
                ("feedback", "render_document_feedback"),
            ):
                item = {
                    "id": item_id,
                    "type": "mcpToolCall",
                    "tool": tool,
                    "arguments": {"path": "/data/deck.pptx"},
                }
                yield {"method": "item/started", "params": {"item": item}}
                yield {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            **item,
                            "status": "completed",
                            "result": {"ok": True},
                        }
                    },
                }
            yield {
                "method": "turn/completed",
                "params": {
                    "turn": {"id": "codex-turn-1", "status": "completed"}
                },
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._completion_file_hash",
        lambda path: "stable" if path == "/data/deck.pptx" else None,
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.CodexMcpHubGateway",
        FakeGateway,
    )

    client = FakeAppServer()
    channel = _Channel()
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="platform-turn",
        runtime_type="codex",
        runtime_session_id="runtime-session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "Create a presentation"},
        model=_BROKER_MODEL,
        command_context={
            "active_modes": ["document"],
            "activated_this_turn": ["document"],
        },
        instructions=[{
            "instruction_id": "command:document:v1",
            "kind": "command_context",
            "scope": "chat",
            "name": "document",
            "version": 1,
            "content": "Create and review the document.",
            "activated_this_turn": True,
        }],
    )

    await run_codex_turn(channel, request, client=client, close_client=False)

    turn_starts = [
        params for method, params in client.requests if method == "turn/start"
    ]
    assert len(turn_starts) == 1
    assert gateway_calls == [
        ("render_interactive", {"path": "/data/deck.pptx"}),
    ]
    events = [message["event"] for message in channel.sent if "event" in message]
    preview = next(
        event for event in events
        if event["type"] == "tool.end"
        and event["payload"]["name"] == "render_interactive"
    )
    assert preview["payload"]["status"] == "done"
    assert preview["payload"]["artifact"]["payload"]["artifact"][
        "artifact_id"
    ] == "ia_completion_preview"
    assert events[-1]["type"] == "runtime.completed"


@pytest.mark.asyncio
async def test_codex_reconciles_broker_turn_after_final_message_without_terminal(
    monkeypatch,
):
    """A compatible provider must not leave a completed answer running forever."""

    class FakeAppServer:
        def __init__(self):
            self.requests: list[tuple[str, dict]] = []
            self._never = asyncio.Event()

        async def start(self):
            return None

        async def request(self, method, params, **_kwargs):
            self.requests.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "codex-thread", "turns": []}}
            if method == "turn/start":
                return {"turn": {"id": "codex-turn"}}
            if method == "thread/turns/list":
                return {
                    "data": [{
                        "id": "codex-turn",
                        "status": "inProgress",
                        "items": [],
                    }],
                    "nextCursor": None,
                }
            if method == "turn/interrupt":
                return {}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "item/started",
                "params": {"item": {"id": "answer", "type": "agentMessage"}},
            }
            yield {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "answer",
                        "type": "agentMessage",
                        "text": "The document is ready.",
                    }
                },
            }
            await self._never.wait()

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex."
        "_BROKER_TERMINAL_RECONCILIATION_IDLE_S",
        0.01,
    )

    client = FakeAppServer()
    channel = _Channel()
    await run_codex_turn(
        channel,
        RuntimeTurnRequest(
            tenant_id="tenant",
            user_id="user",
            chat_id="chat",
            turn_id="platform-turn",
            runtime_type="codex",
            runtime_session_id="runtime-session",
            runtime_root="/runtime/.codex",
            message={"role": "user", "content": "Create the document"},
            model=_BROKER_MODEL,
        ),
        client=client,
        close_client=False,
    )

    assert [method for method, _params in client.requests][-2:] == [
        "thread/turns/list",
        "turn/interrupt",
    ]
    assert any(
        message.get("event", {}).get("type") == "message.end"
        for message in channel.sent
    )
    assert any(
        message.get("event", {}).get("type") == "runtime.completed"
        for message in channel.sent
    )
    assert channel.sent[-1] == {"type": MSG_RUNTIME_RESULT}


@pytest.mark.asyncio
async def test_codex_reuses_one_aggregate_hub_endpoint_across_turns(monkeypatch):
    class FakeAppServer:
        def __init__(self):
            self.requests: list[tuple[str, dict]] = []
            self.turn_number = 0

        async def start(self):
            return None

        async def request(self, method, params, **_kwargs):
            self.requests.append((method, params))
            if method == "thread/start":
                servers = params["config"]["mcp_servers"]
                assert list(servers) == ["flowork"]
                assert servers["flowork"]["url"].startswith(
                    "http://127.0.0.1:"
                )
                return {"thread": {"id": "codex-hub-thread", "turns": []}}
            if method == "turn/start":
                self.turn_number += 1
                return {"turn": {"id": f"codex-hub-turn-{self.turn_number}"}}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "agent-message",
                        "type": "agentMessage",
                        "text": "Completed response",
                    }
                },
            }
            yield {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "id": f"codex-hub-turn-{self.turn_number}",
                        "status": "completed",
                    }
                },
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )

    async def unused_host_gateway(*_args, **_kwargs):
        raise AssertionError("an empty Hub must not call the Host Gateway")

    desired = McpDesiredState(
        organization_id="tenant",
        user_id="user",
        chat_id="chat",
        runtime_session_id="runtime-session",
        sandbox_id="sandbox",
        sandbox_generation=1,
        chat_mcp_config_revision=0,
        platform_contract_revision="platform-1",
        skill_catalog_revision="skills-1",
        servers=[],
    )

    def execution(turn_id: str) -> McpExecutionContext:
        now = datetime.now(timezone.utc)
        return McpExecutionContext(
            organization_id="tenant",
            user_id="user",
            chat_id="chat",
            runtime_session_id="runtime-session",
            sandbox_generation=1,
            turn_id=turn_id,
            agent_run_id=turn_id,
            active_platform_capabilities=[],
            selected_mcp_revision=0,
            approval_mode="agent",
            surface="main",
            authorization_generation="auth-1",
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
            capability="test-capability",
        )

    adapter = SandboxMcpRuntimeAdapter(unused_host_gateway)
    hub = SandboxMcpHub(adapter)
    hub_gateways: dict[str, object] = {}
    resident_threads: dict[str, str] = {}
    client = FakeAppServer()
    base_request = dict(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        runtime_type="codex",
        runtime_session_id="runtime-session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "hello"},
        model=_BROKER_MODEL,
        mcp_runtime_stage="sandbox",
        mcp_desired_state=desired,
    )
    try:
        first_gateway = None
        for turn_number in range(1, 6):
            turn_id = f"platform-turn-{turn_number}"
            await run_codex_turn(
                _Channel(),
                RuntimeTurnRequest(
                    turn_id=turn_id,
                    runtime_state_ref=(
                        "codex-hub-thread" if turn_number > 1 else None
                    ),
                    mcp_execution_context=execution(turn_id),
                    **base_request,
                ),
                client=client,
                close_client=False,
                mcp_hub=hub,
                mcp_adapter=adapter,
                hub_gateway_registry=hub_gateways,
                resident_threads=resident_threads,
            )
            if first_gateway is None:
                first_gateway = hub_gateways["aggregate"]
            assert hub_gateways["aggregate"] is first_gateway

        assert [method for method, _params in client.requests] == [
            "thread/start",
            "turn/start",
            "turn/start",
            "turn/start",
            "turn/start",
            "turn/start",
        ]
    finally:
        gateway = hub_gateways.get("aggregate")
        if gateway is not None:
            await gateway.close()
        await hub.close()


@pytest.mark.asyncio
async def test_codex_forks_loaded_thread_when_turn_config_changes(monkeypatch):
    class FakeAppServer:
        def __init__(self):
            self.requests: list[tuple[str, dict]] = []

        async def start(self):
            return None

        async def request(self, method, params, **_kwargs):
            self.requests.append((method, params))
            if method == "thread/fork":
                assert params["threadId"] == "codex-thread-old"
                assert (
                    params["config"]["model_provider"]
                    == "vibecanvas_runtime_model"
                )
                return {"thread": {"id": "codex-thread-refreshed"}}
            if method == "turn/start":
                assert params["threadId"] == "codex-thread-refreshed"
                return {"turn": {"id": "codex-turn"}}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "agent-message",
                        "type": "agentMessage",
                        "text": "Completed response",
                    }
                },
            }
            yield {
                "method": "turn/completed",
                "params": {"turn": {"id": "codex-turn", "status": "completed"}},
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )

    client = FakeAppServer()
    resident_threads = {"codex-thread-old": "previous-turn-config"}
    channel = _Channel()
    await run_codex_turn(
        channel,
        RuntimeTurnRequest(
            tenant_id="tenant",
            user_id="user",
            chat_id="chat",
            turn_id="platform-turn",
            runtime_type="codex",
            runtime_session_id="runtime-session",
            runtime_root="/runtime/.codex",
            runtime_state_ref="codex-thread-old",
            message={"role": "user", "content": "/workflow create a workflow"},
            model=_BROKER_MODEL,
        ),
        client=client,
        close_client=False,
        resident_threads=resident_threads,
    )

    assert [method for method, _params in client.requests] == [
        "thread/fork",
        "turn/start",
    ]
    assert "codex-thread-old" not in resident_threads
    assert "codex-thread-refreshed" in resident_threads
    checkpoints = [
        message["event"]["payload"]
        for message in channel.sent
        if message.get("event", {}).get("type") == "checkpoint"
    ]
    assert checkpoints == [
        {
            "state_ref": "codex-thread-refreshed",
            "previous_state_ref": "codex-thread-old",
        }
    ]


@pytest.mark.asyncio
async def test_codex_replaces_only_an_explicitly_missing_native_rollout(monkeypatch):
    class FakeAppServer:
        def __init__(self):
            self.requests: list[tuple[str, dict]] = []

        async def start(self):
            return None

        async def request(self, method, params, **_kwargs):
            self.requests.append((method, params))
            if method == "thread/fork":
                raise CodexAppServerError(
                    "codex_app_server_request_failed",
                    "no rollout found for thread id codex-thread-missing",
                )
            if method == "thread/start":
                return {"thread": {"id": "codex-thread-recovered"}}
            if method == "turn/start":
                text = params["input"][0]["text"]
                assert "previous native Codex thread was unavailable" in text
                assert "continue the durable diagram" in text
                return {"turn": {"id": "codex-turn"}}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "agent-message",
                        "type": "agentMessage",
                        "text": "Completed response",
                    }
                },
            }
            yield {
                "method": "turn/completed",
                "params": {"turn": {"id": "codex-turn", "status": "completed"}},
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )

    client = FakeAppServer()
    channel = _Channel()
    await run_codex_turn(
        channel,
        RuntimeTurnRequest(
            tenant_id="tenant",
            user_id="user",
            chat_id="chat",
            turn_id="platform-turn",
            runtime_type="codex",
            runtime_session_id="runtime-session",
            runtime_root="/runtime/.codex",
            runtime_state_ref="codex-thread-missing",
            message={"role": "user", "content": "continue the durable diagram"},
            model=_BROKER_MODEL,
        ),
        client=client,
        close_client=False,
    )

    assert [method for method, _params in client.requests] == [
        "thread/fork",
        "thread/start",
        "turn/start",
    ]
    checkpoints = [
        message["event"]["payload"]
        for message in channel.sent
        if message.get("event", {}).get("type") == "checkpoint"
    ]
    assert checkpoints == [
        {
            "state_ref": "codex-thread-recovered",
            "previous_state_ref": "codex-thread-missing",
        }
    ]


@pytest.mark.asyncio
async def test_codex_rebuilds_partial_native_history_from_durable_transcript(
    monkeypatch,
):
    class FakeAppServer:
        def __init__(self):
            self.requests: list[tuple[str, dict]] = []

        async def start(self):
            return None

        async def request(self, method, params, **_kwargs):
            self.requests.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "codex-thread-rebuilt"}}
            if method == "turn/start":
                text = params["input"][0]["text"]
                assert "authoritative prior conversation" in text
                assert "EARLY-DURABLE-MARKER" in text
                assert "current request" in text
                return {"turn": {"id": "codex-turn"}}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "agent-message",
                        "type": "agentMessage",
                        "text": "Completed response",
                    }
                },
            }
            yield {
                "method": "turn/completed",
                "params": {"turn": {"id": "codex-turn", "status": "completed"}},
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._read_history_coverage",
        lambda _root: {},
    )
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._write_history_coverage",
        lambda _root, *, thread_id, turn_id: writes.append((thread_id, turn_id)),
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )

    client = FakeAppServer()
    await run_codex_turn(
        _Channel(),
        RuntimeTurnRequest(
            tenant_id="tenant",
            user_id="user",
            chat_id="chat",
            turn_id="platform-current-turn",
            runtime_type="codex",
            runtime_session_id="runtime-session",
            runtime_root="/runtime/.codex",
            runtime_state_ref="codex-thread-partial",
            durable_history={
                "last_turn_id": "platform-previous-turn",
                "messages": [{
                    "message_id": "m-early",
                    "turn_id": "platform-previous-turn",
                    "role": "user",
                    "text": "EARLY-DURABLE-MARKER",
                }],
            },
            message={"role": "user", "content": "current request"},
            model=_BROKER_MODEL,
        ),
        client=client,
        close_client=False,
        resident_threads={"codex-thread-partial": "stale-config"},
    )

    assert [method for method, _params in client.requests] == [
        "thread/start",
        "turn/start",
    ]
    assert writes == [("codex-thread-rebuilt", "platform-current-turn")]


def test_codex_history_coverage_file_is_atomic_and_bounded(tmp_path, monkeypatch):
    coverage_path = tmp_path / "coverage.json"
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._history_coverage_path",
        lambda _root: str(coverage_path),
    )

    _write_history_coverage(
        "/runtime/.codex",
        thread_id="native-thread-a",
        turn_id="product-turn-a",
    )
    _write_history_coverage(
        "/runtime/.codex",
        thread_id="native-thread-b",
        turn_id="product-turn-b",
    )

    assert _read_history_coverage("/runtime/.codex") == {
        "native-thread-a": "product-turn-a",
        "native-thread-b": "product-turn-b",
    }
    assert coverage_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_codex_matching_history_watermark_keeps_native_transcript(monkeypatch):
    class FakeAppServer:
        def __init__(self):
            self.requests: list[tuple[str, dict]] = []

        async def start(self):
            return None

        async def request(self, method, params, **_kwargs):
            self.requests.append((method, params))
            if method == "thread/fork":
                return {"thread": {"id": "native-fork"}}
            if method == "turn/start":
                assert "durable-conversation-history" not in params["input"][0]["text"]
                return {"turn": {"id": "native-turn"}}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "agent-message",
                        "type": "agentMessage",
                        "text": "Completed response",
                    }
                },
            }
            yield {
                "method": "turn/completed",
                "params": {"turn": {"id": "native-turn", "status": "completed"}},
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._read_history_coverage",
        lambda _root: {"native-old": "product-prior"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )

    client = FakeAppServer()
    await run_codex_turn(
        _Channel(),
        RuntimeTurnRequest(
            tenant_id="tenant",
            user_id="user",
            chat_id="chat",
            turn_id="product-current",
            runtime_type="codex",
            runtime_session_id="runtime-session",
            runtime_root="/runtime/.codex",
            runtime_state_ref="native-old",
            durable_history={
                "last_turn_id": "product-prior",
                "messages": [{
                    "message_id": "message-prior",
                    "turn_id": "product-prior",
                    "role": "user",
                    "text": "already native",
                }],
            },
            message={"role": "user", "content": "continue"},
            model=_BROKER_MODEL,
        ),
        client=client,
        close_client=False,
    )

    assert [method for method, _params in client.requests] == [
        "thread/fork",
        "turn/start",
    ]


@pytest.mark.asyncio
async def test_codex_failed_turn_does_not_advance_history_watermark(monkeypatch):
    class FakeAppServer:
        async def start(self):
            return None

        async def request(self, method, _params, **_kwargs):
            if method == "thread/start":
                return {"thread": {"id": "native-recovery"}}
            if method == "turn/start":
                return {"turn": {"id": "native-failed"}}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "id": "native-failed",
                        "status": "failed",
                        "error": {"message": "provider interrupted"},
                    }
                },
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._read_history_coverage",
        lambda _root: {},
    )
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._write_history_coverage",
        lambda _root, *, thread_id, turn_id: writes.append((thread_id, turn_id)),
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="provider interrupted"):
        await run_codex_turn(
            _Channel(),
            RuntimeTurnRequest(
                tenant_id="tenant",
                user_id="user",
                chat_id="chat",
                turn_id="product-current",
                runtime_type="codex",
                runtime_session_id="runtime-session",
                runtime_root="/runtime/.codex",
                runtime_state_ref="native-partial",
                durable_history={
                    "last_turn_id": "product-prior",
                    "messages": [{
                        "message_id": "message-prior",
                        "turn_id": "product-prior",
                        "role": "user",
                        "text": "recover me",
                    }],
                },
                message={"role": "user", "content": "continue"},
                model=_BROKER_MODEL,
            ),
            client=FakeAppServer(),
            close_client=False,
        )

    assert writes == []


class _ApprovalChannel:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.controls: asyncio.Queue[dict] = asyncio.Queue()

    async def send(self, message: dict) -> None:
        self.sent.append(message)
        event = message.get("event") or {}
        if event.get("type") != "approval.requested":
            return
        correlation = event["payload"]["runtime_correlation"]
        await self.controls.put({
            "type": "runtime_control",
            "response": {
                "request_id": event["payload"]["hitl_request_id"],
                "chat_id": event["chat_id"],
                "turn_id": event["turn_id"],
                "gate_type": "pre_tool_approval",
                "action": "approve",
                "persisted": True,
                "payload": {},
                "correlation": correlation,
            },
        })

    async def recv(self):
        return await self.controls.get()


class _InteractionChannel:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.controls: asyncio.Queue[dict] = asyncio.Queue()

    async def send(self, message: dict) -> None:
        self.sent.append(message)
        event = message.get("event") or {}
        if event.get("type") != "interaction.required":
            return
        payload = event["payload"]
        await self.controls.put({
            "type": "runtime_control",
            "response": {
                "request_id": payload["hitl_request_id"],
                "chat_id": event["chat_id"],
                "turn_id": event["turn_id"],
                "gate_type": "post_tool_interaction",
                "action": "submit",
                "persisted": True,
                "payload": {
                    "interaction_result": {
                        "widget_state": {"scope": "Workspace"},
                    }
                },
                "correlation": payload["runtime_correlation"],
            },
        })

    async def recv(self):
        return await self.controls.get()


@pytest.mark.asyncio
async def test_codex_request_user_input_resumes_the_same_native_turn(monkeypatch):
    instances = []

    class FakeAppServer:
        def __init__(self, **_kwargs):
            self.responses = []
            self.errors = []
            instances.append(self)

        async def start(self):
            return None

        async def request(self, method, _params, **_kwargs):
            if method == "thread/start":
                return {"thread": {"id": "codex-thread"}}
            if method == "turn/start":
                return {"turn": {"id": "codex-turn"}}
            raise AssertionError(method)

        async def respond(self, request_id, result):
            self.responses.append((request_id, result))

        async def respond_error(self, request_id, *, code, message):
            self.errors.append((request_id, code, message))

        async def messages(self):
            yield {
                "id": 73,
                "method": "item/tool/requestUserInput",
                "params": {
                    "threadId": "codex-thread",
                    "turnId": "codex-turn",
                    "itemId": "input-item-1",
                    "questions": [
                        {
                            "id": "scope",
                            "header": "Scope",
                            "question": "Which scope?",
                            "options": [
                                {"label": "Current file"},
                                {"label": "Workspace"},
                            ],
                        }
                    ],
                },
            }
            assert self.responses == [
                (73, {"answers": {"scope": {"answers": ["Workspace"]}}})
            ]
            yield {
                "id": 74,
                "method": "future/nativeInteraction",
                "params": {"private": "must-not-be-projected"},
            }
            assert self.errors == [
                (
                    74,
                    -32601,
                    "This Codex request is not supported by Flowork yet.",
                )
            ]
            yield {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "codex-thread",
                    "turnId": "codex-turn",
                    "tokenUsage": {
                        "last": {
                            "inputTokens": 120,
                            "cachedInputTokens": 20,
                            "outputTokens": 30,
                            "reasoningOutputTokens": 5,
                            "totalTokens": 150,
                        },
                        "total": {
                            "inputTokens": 120,
                            "cachedInputTokens": 20,
                            "outputTokens": 30,
                            "reasoningOutputTokens": 5,
                            "totalTokens": 150,
                        },
                        "modelContextWindow": 200000,
                    },
                },
            }
            yield {
                "method": "turn/completed",
                "params": {"turn": {"id": "codex-turn", "status": "completed"}},
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.CodexAppServer",
        FakeAppServer,
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._codex_executable",
        lambda: "/codex/bin/codex.js",
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )

    channel = _InteractionChannel()
    await run_codex_turn(
        channel,
        RuntimeTurnRequest(
            tenant_id="tenant",
            user_id="user",
            chat_id="chat",
            turn_id="platform-turn",
            runtime_type="codex",
            runtime_session_id="runtime-session",
            runtime_root="/runtime/.codex",
            message={"role": "user", "content": "ask me"},
            model=_BROKER_MODEL,
        ),
    )

    events = [message["event"] for message in channel.sent if "event" in message]
    assert [event["type"] for event in events] == [
        "runtime.started",
        "checkpoint",
        "message.start",
        "tool.start",
        "message.end",
        "interaction.required",
        "tool.end",
        "interaction.resolved",
        "projection",
        "projection",
        "usage",
        "runtime.completed",
    ]
    interaction = events[5]["payload"]
    assert interaction["resume_mode"] == "same_turn"
    assert interaction["interaction_definition"]["component_type"] == "user_input"
    assert interaction["runtime_correlation"]["runtime_request_id"] == 73
    assert events[6]["payload"]["status"] == "done"
    assert events[8]["payload"]["payload"]["code"] == (
        "codex_request_unknown"
    )
    assert "private" not in json.dumps(events[8])
    assert events[10]["payload"] == {
        "model": "gpt-codex-current",
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "cached_input_tokens": 20,
        "reasoning_output_tokens": 5,
        "total_tokens": 150,
        "context_window_tokens": 200000,
        "native_kind": "thread/tokenUsage/updated",
    }
    assert instances[0].responses == [
        (73, {"answers": {"scope": {"answers": ["Workspace"]}}})
    ]


@pytest.mark.asyncio
async def test_codex_aggregate_hub_emits_runtime_neutral_approval(monkeypatch):
    gateways = []

    class FakeGateway:
        def __init__(self, _hub, _adapter):
            self.request_approval = None
            self.url = None
            gateways.append(self)

        async def activate(
            self,
            *,
            desired_servers,
            request_approval,
            requires_approval,
        ):
            del desired_servers, requires_approval
            self.request_approval = request_approval
            self.url = f"http://127.0.0.1:{43210 + len(gateways)}/"
            return []

        def deactivate(self):
            return None

        async def close(self):
            return None

    class FakeAppServer:
        def __init__(self, **_kwargs):
            pass

        async def start(self):
            return None

        async def request(self, method, _params, **_kwargs):
            if method == "thread/start":
                servers = _params["config"]["mcp_servers"]
                assert set(servers) == {"flowork"}
                assert all(
                    server["url"].startswith("http://127.0.0.1:")
                    and "http_headers" not in server
                    for server in servers.values()
                )
                return {"thread": {"id": "codex-thread"}}
            if method == "turn/start":
                return {"turn": {"id": "codex-turn"}}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "item/started",
                "params": {
                    "item": {
                        "id": "exec-browser-click-1",
                        "type": "mcpToolCall",
                        "tool": "browser_click",
                        "arguments": {
                            "handle": "submit",
                            "require_user_auth": True,
                            "approval_reason": "Submit the form",
                        },
                    }
                },
            }
            assert gateways[0].request_approval is not None
            action = await gateways[0].request_approval(
                "browser_click",
                {
                    "handle": "submit",
                    "require_user_auth": True,
                    "approval_reason": "Submit the form",
                },
                "gateway-call-1",
            )
            assert action == "approve"
            yield {
                "method": "turn/completed",
                "params": {"turn": {"id": "codex-turn", "status": "completed"}},
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.CodexMcpHubGateway",
        FakeGateway,
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.CodexAppServer",
        FakeAppServer,
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._codex_executable",
        lambda: "/codex/bin/codex.js",
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )
    channel = _ApprovalChannel()
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="platform-turn",
        runtime_type="codex",
        runtime_session_id="runtime-session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "/browser submit"},
        model=_BROKER_MODEL,
        approval_mode="agent",
        active_platform_mcps=["config", "interactive", "workflow", "browser"],
        mcp_host_servers=[
            *[
                {
                    "name": name,
                    "source": "platform",
                    "connection": {
                        "transport": "host_gateway",
                        "capability": f"{name}-private",
                    },
                }
                for name in ("config", "interactive", "workflow")
            ],
            {
                "name": "browser",
                "source": "platform",
                "connection": {
                    "transport": "browser_gateway",
                    "capability": "private",
                },
            }
        ],
    )

    await run_codex_turn(channel, request)

    assert len(gateways) == 1

    events = [message["event"] for message in channel.sent if "event" in message]
    assert [event["type"] for event in events] == [
        "runtime.started",
        "checkpoint",
        "message.start",
        "tool.start",
        "message.end",
        "approval.requested",
        "approval.resolved",
        "runtime.completed",
    ]
    required = events[5]["payload"]
    assert required["agent_payload"]["tool"] == "browser_click"
    assert required["prompt_text"] == "Submit the form"
    assert required["runtime_correlation"] == {
        "source": "platform_mcp",
        "runtime_request_id": "gateway-call-1",
        "runtime_method": "tools/call",
        "runtime_thread_id": "codex-thread",
        "runtime_turn_id": "codex-turn",
        "runtime_item_id": "exec-browser-click-1",
    }
    assert events[6]["payload"]["status"] == "approved"


@pytest.mark.asyncio
async def test_codex_runtime_translates_app_server_stream_to_stable_events(monkeypatch):
    instances = []
    captured_snapshots = []

    class FakeAppServer:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.requests = []
            instances.append(self)

        async def start(self):
            return None

        async def request(self, method, params, **_kwargs):
            self.requests.append((method, params))
            if method == "thread/start":
                return {
                    "thread": {
                        "id": "codex-thread",
                        "turns": [],
                    }
                }
            if method == "turn/start":
                return {"turn": {"id": "codex-turn"}}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "mcpServer/startupStatus/updated",
                "params": {"name": "diagram", "status": "starting"},
            }
            yield {
                "method": "mcpServer/startupStatus/updated",
                "params": {"name": "diagram", "status": "ready"},
            }
            yield {
                "method": "hook/started",
                "params": {
                    "threadId": "codex-thread",
                    "turnId": "codex-turn",
                    "run": {
                        "id": "hook-1",
                        "eventName": "postToolUse",
                        "scope": "turn",
                        "executionMode": "sync",
                        "status": "running",
                        "sourcePath": "/host/private/hook.py",
                        "entries": [],
                    },
                },
            }
            yield {
                "method": "hook/completed",
                "params": {
                    "threadId": "codex-thread",
                    "turnId": "codex-turn",
                    "run": {
                        "id": "hook-1",
                        "eventName": "postToolUse",
                        "scope": "turn",
                        "executionMode": "sync",
                        "status": "completed",
                        "statusMessage": "Validation complete",
                        "sourcePath": "/host/private/hook.py",
                        "entries": [{"text": "private output"}],
                    },
                },
            }
            yield {
                "method": "item/started",
                "params": {"item": {"id": "message-1", "type": "agentMessage"}},
            }
            yield {
                "method": "item/agentMessage/delta",
                "params": {"itemId": "message-1", "delta": "hel"},
            }
            yield {
                "method": "item/agentMessage/delta",
                "params": {"itemId": "message-1", "delta": "lo"},
            }
            yield {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "message-1",
                        "type": "agentMessage",
                        "text": "hello",
                    }
                },
            }
            yield {
                "method": "turn/completed",
                "params": {"turn": {"id": "codex-turn", "status": "completed"}},
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.CodexAppServer",
        FakeAppServer,
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._codex_executable",
        lambda: "/codex/bin/codex.js",
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setenv("AGENT_DEBUG_VIEW_ENABLED", "1")
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.capture_codex_debug_snapshot",
        lambda **kwargs: captured_snapshots.append(kwargs) or "/logs/.debug/snapshot.json",
    )
    channel = _Channel()
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="platform-turn",
        runtime_type="codex",
        runtime_session_id="runtime-session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "say hello"},
        model=_BROKER_MODEL,
        reasoning_effort="high",
        command_context={
            "is_first": True,
            "active_modes": ["browser"],
            "activated_this_turn": ["browser"],
        },
        instructions=[{
            "instruction_id": "command:browser:v1",
            "kind": "command_context",
            "scope": "chat",
            "name": "browser",
            "version": 1,
            "content": "BACKEND-RESOLVED BROWSER CONTEXT",
            "activated_this_turn": True,
        }],
    )

    await run_codex_turn(channel, request)

    events = [message["event"] for message in channel.sent if "event" in message]
    assert [event["type"] for event in events] == [
        "runtime.started",
        "checkpoint",
        "message.start",
        "tool.start",
        "message.end",
        "tool.end",
        "message.start",
        "tool.start",
        "message.end",
        "tool.end",
        "message.start",
        "message.delta",
        "message.delta",
        "message.end",
        "runtime.completed",
    ]
    started = events[0]["payload"]
    assert started["first_turn"] is True
    assert started["mcp_server_count"] == 0
    assert set(started["timings_ms"]) == {
        "skills_prepare_ms",
        "app_server_start_ms",
        "mcp_gateway_start_ms",
        "mcp_config_ms",
        "thread_open_ms",
        "turn_start_ms",
        "setup_total_ms",
    }
    assert all(
        isinstance(value, int) and value >= 0
        for value in started["timings_ms"].values()
    )
    assert "say hello" not in str(started)
    assert _BROKER_MODEL["api_key"] not in str(started)
    assert events[1]["payload"] == {"state_ref": "codex-thread"}
    assert [events[11]["payload"]["delta"], events[12]["payload"]["delta"]] == [
        "hel",
        "lo",
    ]
    mcp_invocation = events[5]["payload"]["invocation"]
    assert mcp_invocation["nativeKind"] == "mcpToolCall"
    hook_invocation = events[9]["payload"]["invocation"]
    assert hook_invocation["nativeKind"] == "hookPrompt"
    assert "/host/private" not in json.dumps(events)
    assert "private output" not in json.dumps(events)
    turn_start = next(
        params for method, params in instances[0].requests if method == "turn/start"
    )
    assert turn_start["model"] == "gpt-codex-current"
    assert turn_start["effort"] == "high"
    assert "BACKEND-RESOLVED BROWSER CONTEXT" in turn_start["input"][0]["text"]
    assert "<user-message>\nsay hello\n</user-message>" in turn_start["input"][0]["text"]
    assert len(captured_snapshots) == 1
    assert captured_snapshots[0]["thread"] == {
        "id": "codex-thread",
        "turns": [],
    }
    assert captured_snapshots[0]["thread_id"] == "codex-thread"
    assert captured_snapshots[0]["current_input"] == turn_start["input"]
    assert channel.sent[-1] == {"type": MSG_RUNTIME_RESULT}


@pytest.mark.asyncio
async def test_codex_runtime_projects_full_plan_snapshot_as_todo_update(monkeypatch):
    class FakeAppServer:
        def __init__(self, **_kwargs):
            pass

        async def start(self):
            return None

        async def request(self, method, _params, **_kwargs):
            if method == "thread/start":
                return {"thread": {"id": "codex-thread"}}
            if method == "turn/start":
                return {"turn": {"id": "codex-turn"}}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "turn/plan/updated",
                "params": {
                    "threadId": "codex-thread",
                    "turnId": "codex-turn",
                    "explanation": "Implementation plan",
                    "plan": [
                        {"step": "Inspect files", "status": "completed"},
                        {"step": "Implement change", "status": "inProgress"},
                        {"step": "Run tests", "status": "pending"},
                    ],
                },
            }
            yield {
                "method": "turn/completed",
                "params": {"turn": {"id": "codex-turn", "status": "completed"}},
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.CodexAppServer",
        FakeAppServer,
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._codex_executable",
        lambda: "/codex/bin/codex.js",
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )
    channel = _Channel()
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="platform-turn",
        runtime_type="codex",
        runtime_session_id="runtime-session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "implement this"},
        model=_BROKER_MODEL,
    )

    await run_codex_turn(channel, request)

    projection = next(
        message["event"]
        for message in channel.sent
        if message.get("event", {}).get("type") == "projection"
    )
    assert projection["payload"] == {
        "event_type": "CHAT_EVENT",
        "payload": {
            "type": "todo_update",
            "items": [
                {"id": 1, "text": "Inspect files", "status": "done"},
                {"id": 2, "text": "Implement change", "status": "in_progress"},
                {"id": 3, "text": "Run tests", "status": "pending"},
            ],
        },
    }


@pytest.mark.asyncio
async def test_codex_runtime_closes_tool_carrier_before_tool_result(monkeypatch):
    class FakeAppServer:
        def __init__(self, **_kwargs):
            pass

        async def start(self):
            return None

        async def request(self, method, _params, **_kwargs):
            if method == "thread/start":
                return {"thread": {"id": "codex-thread"}}
            if method == "turn/start":
                return {"turn": {"id": "codex-turn"}}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "item/started",
                "params": {
                    "item": {
                        "id": "command-1",
                        "type": "commandExecution",
                        "command": "printf ok",
                        "cwd": "/data",
                    }
                },
            }
            yield {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "command-1",
                        "type": "commandExecution",
                        "command": "printf ok",
                        "cwd": "/data",
                        "status": "completed",
                        "aggregatedOutput": "ok",
                    }
                },
            }
            yield {
                "method": "turn/completed",
                "params": {"turn": {"id": "codex-turn", "status": "completed"}},
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.CodexAppServer",
        FakeAppServer,
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._codex_executable",
        lambda: "/codex/bin/codex.js",
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )
    channel = _Channel()
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="platform-turn",
        runtime_type="codex",
        runtime_session_id="runtime-session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "run a command"},
        model=_BROKER_MODEL,
    )

    await run_codex_turn(channel, request)

    events = [message["event"] for message in channel.sent if "event" in message]
    assert [event["type"] for event in events] == [
        "runtime.started",
        "checkpoint",
        "message.start",
        "tool.start",
        "message.end",
        "tool.end",
        "runtime.completed",
    ]
    carrier_id = "codex-tool:codex-turn:command-1"
    assert events[2]["payload"]["message_id"] == carrier_id
    assert events[3]["payload"]["message_id"] == carrier_id
    assert events[4]["payload"]["message_id"] == carrier_id


@pytest.mark.asyncio
async def test_codex_render_interactive_gate_ends_turn_at_completed_tool_boundary(
    monkeypatch,
):
    instances = []
    envelope = {
        "status": "success",
        "content": "rendered",
        "payload": {
            "kind": "interactive_artifact",
            "artifact": {
                "kind": "interactive_artifact",
                "artifact_id": "ia_codex_continue",
                "title": "Review",
                "component_type": "html_preview",
                "completion_mode": "wait_for_submit",
                "require_human_confirm": True,
                "interaction_schema": {
                    "interaction_type": "continue",
                    "submit_label": "Continue",
                },
                "interaction_state": {
                    "status": "awaiting_loop_gate",
                    "is_interacted": False,
                    "result": {},
                },
            },
        },
        "meta": {"tool": "render_interactive"},
    }

    class FakeAppServer:
        def __init__(self, **_kwargs):
            self.requests = []
            instances.append(self)

        async def start(self):
            return None

        async def request(self, method, _params, **_kwargs):
            self.requests.append(method)
            if method == "thread/start":
                return {"thread": {"id": "codex-thread"}}
            if method == "turn/start":
                return {"turn": {"id": "codex-turn"}}
            if method == "turn/interrupt":
                return {}
            raise AssertionError(method)

        async def messages(self):
            yield {
                "method": "item/started",
                "params": {
                    "item": {
                        "id": "interactive-1",
                        "type": "mcpToolCall",
                        "tool": "render_interactive",
                        "arguments": {"require_human_confirm": True},
                    }
                },
            }
            yield {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "interactive-1",
                        "type": "mcpToolCall",
                        "tool": "render_interactive",
                        "arguments": {"require_human_confirm": True},
                        "status": "completed",
                        "result": {"structuredContent": envelope},
                    }
                },
            }
            raise AssertionError("adapter must stop before another model step")

        async def close(self):
            return None

    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.CodexAppServer",
        FakeAppServer,
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex._codex_executable",
        lambda: "/codex/bin/codex.js",
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.path.isdir",
        lambda path: path in {"/runtime", "/data"},
    )
    monkeypatch.setattr(
        "vibecanvas_api.services.agent_runtime.codex.os.makedirs",
        lambda *_args, **_kwargs: None,
    )

    channel = _Channel()
    request = RuntimeTurnRequest(
        tenant_id="tenant",
        user_id="user",
        chat_id="chat",
        turn_id="platform-turn",
        runtime_type="codex",
        runtime_session_id="runtime-session",
        runtime_root="/runtime/.codex",
        message={"role": "user", "content": "render and wait"},
        model=_BROKER_MODEL,
    )
    await run_codex_turn(channel, request)

    events = [message["event"] for message in channel.sent if "event" in message]
    assert [event["type"] for event in events] == [
        "runtime.started",
        "checkpoint",
        "message.start",
        "tool.start",
        "message.end",
        "interaction.required",
        "tool.end",
        "runtime.completed",
    ]
    required = events[5]["payload"]
    assert required["hitl_type"] == "post_tool_review"
    assert required["agent_payload"]["resume_mode"] == "new_turn"
    assert events[6]["payload"]["artifact"]["payload"]["hitl_request_id"].startswith(
        "hitl_"
    )
    assert "turn/interrupt" in instances[0].requests
