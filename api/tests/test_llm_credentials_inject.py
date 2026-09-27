# -*- coding: utf-8 -*-
"""Workflow model-broker descriptor construction.

DB-free: ``build_llm_credentials_extra`` is exercised against a fake
``LlmCredentialsRepo.list_for_user`` so the scan, name filter and secretless
capability shape are tested without Postgres. The engine-side resolution is covered by
``engine/tests/test_prompt_injected_credentials.py``.
"""
from __future__ import annotations

import pytest

from vibecanvas_api.services import llm_credentials_inject as inj
from vibecanvas_api.services.agent_runtime.workflow_model_capability import (
    verify_runtime_workflow_model_capability,
)


def _wf(*prompt_model_names: str) -> dict:
    """A flat workflow dict: a StartNode + one PromptNode per supplied
    model_name (plus a non-PromptNode that must be ignored)."""
    wf: dict = {
        "__meta__": {"workflow_id": "wf1"},
        "node_1": {
            "node_id": "node_1", "node_name": "__start__",
            "node_type": "StartNode", "node_config": {},
        },
        "node_code": {
            "node_id": "node_code", "node_name": "c",
            "node_type": "CodeNode",
            "node_config": {"model_name": "not-a-prompt-node"},
        },
    }
    for i, name in enumerate(prompt_model_names, start=2):
        nid = f"node_p{i}"
        wf[nid] = {
            "node_id": nid, "node_name": f"p{i}",
            "node_type": "PromptNode",
            "node_config": {"model_name": name, "prompt_template": "x"},
        }
    return wf


def test_collect_includes_all_model_names_but_skips_non_model_nodes(monkeypatch):
    # 'OpenAI'/'Gemini' are builtins; pretend 'gpt-5.4' is registry-registered.
    monkeypatch.setattr(inj, "_builtin_model_names",
                        lambda: {"OpenAI", "Gemini", "gpt-5.4"})
    wf = _wf("OpenAI", "gpt-5.4", "My DeepSeek", "Team Claude")
    names = inj.collect_referenced_credential_names(wf)
    # Only the two saved names survive; builtins + the CodeNode are ignored.
    assert names == {"My DeepSeek", "Team Claude", "OpenAI", "gpt-5.4"}


def test_collect_empty_when_no_prompt_nodes(monkeypatch):
    monkeypatch.setattr(inj, "_builtin_model_names", lambda: {"OpenAI", "Gemini"})
    assert inj.collect_referenced_credential_names(_wf()) == set()


def test_collect_includes_subagent_node_model_name(monkeypatch):
    # SubAgentNode stores its model at node_config["model_name"] just like
    # PromptNode; the injector must resolve it the SAME way (saved names only).
    monkeypatch.setattr(inj, "_builtin_model_names",
                        lambda: {"OpenAI", "Gemini"})
    wf = {
        "__meta__": {"workflow_id": "wf1"},
        "node_sa": {
            "node_id": "node_sa", "node_name": "sa",
            "node_type": "SubAgentNode",
            "node_config": {"model_name": "my-saved"},
        },
    }
    assert inj.collect_referenced_credential_names(wf) == {"my-saved"}


def test_collect_dedups_prompt_and_subagent_names(monkeypatch):
    # A PromptNode and a SubAgentNode referencing names (one shared) are both
    # collected and deduped; builtins are still skipped for both node types.
    monkeypatch.setattr(inj, "_builtin_model_names",
                        lambda: {"OpenAI", "Gemini"})
    wf = {
        "__meta__": {"workflow_id": "wf1"},
        "node_p": {
            "node_id": "node_p", "node_name": "p",
            "node_type": "PromptNode",
            "node_config": {"model_name": "shared"},
        },
        "node_sa": {
            "node_id": "node_sa", "node_name": "sa",
            "node_type": "SubAgentNode",
            "node_config": {"model_name": "shared"},
        },
        "node_sa2": {
            "node_id": "node_sa2", "node_name": "sa2",
            "node_type": "SubAgentNode",
            "node_config": {"model_name": "sub-only"},
        },
        "node_builtin": {
            "node_id": "node_builtin", "node_name": "b",
            "node_type": "SubAgentNode",
            "node_config": {"model_name": "OpenAI"},
        },
    }
    assert inj.collect_referenced_credential_names(wf) == {"shared", "sub-only", "OpenAI"}


class _FakeRepo:
    """Stand-in for encrypted LLM credential metadata rows."""

    rows = [
        {"id": "11111111-1111-4111-8111-111111111111",
         "name": "My DeepSeek", "provider": "OpenAI",
         "model_name": "deepseek-chat", "api_url": "https://api.deepseek.com/v1",
         "api_key": "test-only-secret", "proxy": "http://proxy:8080",
         "secret_ref": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
         "updated_at": "v1", "model_context_tokens": 128000},
        {"id": "22222222-2222-4222-8222-222222222222",
         "name": "Team Claude", "provider": "OpenAI",
         "model_name": "claude-via-proxy", "api_url": "https://proxy/v1",
         "api_key": "tk-secret",
         "secret_ref": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
         "updated_at": "v2"},
        {"id": "33333333-3333-4333-8333-333333333333",
         "name": "Unused Key", "provider": "Gemini",
         "model_name": "gemini-2.0-flash", "api_url": "", "api_key": "g-secret",
         "secret_ref": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
         "updated_at": "v3"},
    ]

    def __init__(self, session):
        pass

    async def list_for_user(self, user_id):
        assert user_id == "user-1"
        return [{"connection_kind": "manual", "enabled": True, **row} for row in self.rows]


@pytest.mark.asyncio
async def test_build_mapping_only_for_referenced_names(monkeypatch):
    monkeypatch.setattr(inj, "_builtin_model_names",
                        lambda: {"OpenAI", "Gemini"})
    monkeypatch.setattr(inj, "LlmCredentialsRepo", _FakeRepo)

    wf = _wf("My DeepSeek")
    mapping = await inj.build_llm_credentials_extra(
        wf,
        session=object(),
        organization_id="org-1",
        user_id="user-1",
        workflow_id="wf-1",
        execution_id="execution-1",
        execution_resource_type="workflow_execution",
    )

    # Only the referenced saved name is materialized; 'Unused Key' / 'Team
    # Claude' are not referenced so they are NOT leaked into the mapping.
    assert set(mapping) == {"My DeepSeek"}
    entry = mapping["My DeepSeek"]
    assert entry["provider"] == "openai"
    assert entry["model_name"] == "deepseek-chat"
    assert entry["model_context_tokens"] == 128000
    assert entry["api_url"].endswith("/api/internal/runtime-model/v1")
    assert "proxy" not in entry
    assert "test-only-secret" not in repr(entry)
    assert "api.deepseek.com" not in repr(entry)
    capability = verify_runtime_workflow_model_capability(
        entry["api_key"],
        secret=inj.app_config.signing_secret,
    )
    assert capability is not None
    assert capability.organization_id == "org-1"
    assert capability.user_id == "user-1"
    assert capability.workflow_id == "wf-1"
    assert capability.execution_id == "execution-1"
    assert capability.execution_resource_type == "workflow_execution"
    assert capability.credential_id == "11111111-1111-4111-8111-111111111111"


@pytest.mark.asyncio
async def test_build_empty_when_no_saved_names(monkeypatch):
    monkeypatch.setattr(inj, "_builtin_model_names",
                        lambda: {"OpenAI", "Gemini"})
    monkeypatch.setattr(inj, "LlmCredentialsRepo", _FakeRepo)
    # All builtins → no lookup, empty mapping (caller skips injecting the key).
    mapping = await inj.build_llm_credentials_extra(
        _wf(),
        session=object(),
        organization_id="org-1",
        user_id="user-1",
        workflow_id="wf-1",
        execution_id="execution-1",
        execution_resource_type="workflow_execution",
    )
    assert mapping == {}


@pytest.mark.asyncio
async def test_build_fails_closed_on_repo_error(monkeypatch):
    monkeypatch.setattr(inj, "_builtin_model_names",
                        lambda: {"OpenAI", "Gemini"})

    class _Boom:
        def __init__(self, session):
            pass

        async def list_for_user(self, user_id):
            raise RuntimeError("db down")

    monkeypatch.setattr(inj, "LlmCredentialsRepo", _Boom)
    with pytest.raises(RuntimeError, match="db down"):
        await inj.build_llm_credentials_extra(
            _wf("My DeepSeek"), session=object(), organization_id="org-1",
            user_id="user-1", workflow_id="wf-1", execution_id="execution-1",
            execution_resource_type="workflow_execution",
        )


def test_inline_model_secrets_are_rejected_before_sandbox_launch():
    workflow = _wf("OpenAI")
    workflow["node_p2"]["node_config"]["custom_model_config"] = {
        "api_key": "must-never-enter-sandbox",
        "api_url": "https://provider.example/v1",
    }
    with pytest.raises(ValueError, match="Inline model credentials"):
        inj.reject_inline_model_credentials(workflow)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,changes", [
    ("Default API", {}), ("OpenAI", {}),
    ("My DeepSeek", {"connection_kind": "openrouter_oauth"}),
    ("My DeepSeek", {"enabled": False}),
])
async def test_disallowed_models_fail_before_capability_mint(monkeypatch, name, changes):
    class Repo(_FakeRepo):
        async def list_for_user(self, user_id):
            return [{**row, **changes} for row in await super().list_for_user(user_id)]
    monkeypatch.setattr(inj, "LlmCredentialsRepo", Repo)
    monkeypatch.setattr(inj, "_brokered_entry", lambda **_: pytest.fail("must not mint"))
    with pytest.raises(ValueError, match="manually added APIs"):
        await inj.build_llm_credentials_extra(
            _wf(name), session=object(), organization_id="org-1", user_id="user-1",
            workflow_id="wf-1", execution_id="execution-1", execution_resource_type="workflow_execution",
        )
