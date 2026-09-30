from vibecanvas_engine.nodes.subagent import SubAgentNode


def _subagent_node(node_config: dict) -> dict:
    return {
        "node_id": "node_2",
        "node_name": "worker",
        "node_type": "SubAgentNode",
        "node_description": "Run a bounded sub-agent",
        "input_fields": {
            "file_path": {"type": "string", "value": "", "reference": "__start__.file_path"}
        },
        "output_fields": {
            "summary": {"type": "string", "description": "Findings summary"}
        },
        "node_config": node_config,
        "children": [],
        "__attributes__": {"x": 300, "y": 0},
    }


def test_subagent_node_accepts_task_template_and_model_only():
    node = _subagent_node({
        "task_template": "# Task\nRead {{file_path}} and summarize key points.",
        "model_name": "default-model",
        "max_iterations": 8,
    })

    result = SubAgentNode.check(node)

    assert result["status"] == "success"


def test_subagent_node_rejects_system_prompt_and_inference_config():
    node = _subagent_node({
        "task_template": "Read {{file_path}}.",
        "model_name": "default-model",
        "system_prompt": "be brief",
        "inference_config": {"temperature": 0.2},
    })

    result = SubAgentNode.check(node)

    assert result["status"] == "error"
    assert "Additional properties are not allowed" in result["error_message"]


def test_subagent_node_spec_documents_task_template_shape():
    guide = SubAgentNode.AGENT_SPEC["config_guide"]["task_template"]

    assert "what to inspect" in guide
    assert "what to return" in guide


def test_subagent_model_uses_injected_runtime_broker_capability():
    cfg = SubAgentNode._agent_cfg(
        "FreeKey",
        {
            "llm_credentials": {
                "FreeKey": {
                    "provider": "openai",
                    "model_name": "gpt-4.1-mini",
                    "api_url": "http://platform.test/api/internal/runtime-model/v1",
                    "api_key": "short-lived-broker-capability",
                    "timeout": 45,
                    "model_context_tokens": 128000,
                }
            }
        },
    )

    assert cfg == {
        "model": "openai:gpt-4.1-mini",
        "base_url": "http://platform.test/api/internal/runtime-model/v1",
        "api_key": "short-lived-broker-capability",
        "timeout": 45,
        "model_context_tokens": 128000,
    }


def test_subagent_rejects_incomplete_injected_broker_entry():
    try:
        SubAgentNode._agent_cfg(
            "FreeKey",
            {"llm_credentials": {"FreeKey": {"provider": "openai"}}},
        )
    except RuntimeError as exc:
        assert "incomplete" in str(exc)
    else:  # pragma: no cover - explicit assertion keeps this dependency-free
        raise AssertionError("incomplete broker configuration must be rejected")


def test_subagent_call_exposes_declared_fields_without_nested_envelope(monkeypatch):
    node = SubAgentNode(**_subagent_node({
        "task_template": "Summarize {{file_path}}.",
        "model_name": "FreeKey",
        "max_iterations": 4,
    }))

    async def fake_call_async(_inputs, _extra):
        return {"summary": "verified"}

    monkeypatch.setattr(node, "_call_async", fake_call_async)
    result = node({"file_path": "/run/input.txt"}, {}, extra={})

    assert result["status"] == "success"
    assert result["output"] == {"summary": "verified"}


def test_subagent_accepts_named_latest_resources_and_rejects_ambiguous_refs():
    config = {"task_template": "Audit", "model_name": "manual",
              "skills": [{"id": "skill-1", "name": "Audit"}],
              "mcp_servers": [{"id": "server-1", "name": "Database"}]}
    assert SubAgentNode.check(_subagent_node(config))["status"] == "success"
    config["skills"].append({**config["skills"][0], "name": "Another name"})
    assert SubAgentNode.check(_subagent_node(config))["status"] == "error"
    config["skills"].pop()
    config["mcp_servers"][0]["api_key"] = "must-not-be-stored"
    assert SubAgentNode.check(_subagent_node(config))["status"] == "error"


def test_subagent_skill_reference_does_not_require_or_accept_a_version():
    config = {"task_template": "Audit", "model_name": "manual",
              "skills": [{"id": "skill-1", "name": "Audit"}]}
    assert SubAgentNode.check(_subagent_node(config))["status"] == "success"
    config["skills"][0]["revision_hash"] = "a" * 64
    assert SubAgentNode.check(_subagent_node(config))["status"] == "error"
