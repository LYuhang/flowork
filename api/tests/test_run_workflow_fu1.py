# -*- coding: utf-8 -*-
"""FU-1: run_workflow uses the shared resident workflow sandbox runner."""
import asyncio
import json

import pytest


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_shared_runner_stages_only_broker_descriptor(monkeypatch, tmp_path):
    from vibecanvas_api.services import workflow_sandbox_runner as runner

    run_dir = tmp_path / "workflow-run"
    run_dir.mkdir()
    seen: dict = {}
    broker_entry = {
        "provider": "openai",
        "model_name": "gpt-test",
        "api_url": "http://platform.test/api/internal/runtime-model/v1",
        "api_key": "short-lived-workflow-capability",
        "timeout": 60,
    }

    class FakeSession:
        workflow_run_dir = str(run_dir)
        workflow_run_id = "wf-1"

        async def submit_workflow_stream(self, **kwargs):
            seen.update(kwargs)
            seen["extra_file_exists"] = (run_dir / "__exec__" / "extra.json").exists()
            yield {
                "type": "result",
                "final_outputs": {"end": {"ok": True}},
                "error_dict": {},
                "execution_time": 0.1,
            }

        async def writeback_vfs(self):
            return None

    async def inject(_context, _workflow, tenant_id, **claims):
        assert tenant_id == "org-1"
        assert claims == {
            "user_id": "user-1",
            "workflow_id": "wf-1",
            "execution_id": "execution-1",
            "execution_resource_type": "workflow_execution",
        }
        return {"llm_credentials": {"Saved": broker_entry}}

    monkeypatch.setattr(runner, "inject_into_run_context_async", inject)
    monkeypatch.setattr(
        runner,
        "clear_run_contents",
        lambda *_args, **_kwargs: _async_value(None),
    )
    job = await runner.run_workflow_once(
        FakeSession(),
        tenant_id="org-1",
        workflow={"node_1": {"node_type": "PromptNode"}},
        inputs={"value": 1},
        workflow_run_id="wf-1",
        user_id="user-1",
        workflow_id="wf-1",
        execution_id="execution-1",
        execution_resource_type="workflow_execution",
    )
    assert job.result_json["final_outputs"]["end"]["ok"] is True
    assert seen["extra"] == {
        "llm_credentials": {"Saved": broker_entry},
    }
    assert seen["extra_file_exists"] is False
    staged = json.dumps(seen["extra"])
    assert "provider-secret" not in staged
    assert "provider.example" not in staged


@pytest.mark.asyncio
async def test_terminal_frame_is_visible_only_after_vfs_writeback(tmp_path):
    from vibecanvas_api.services import workflow_sandbox_runner as runner

    run_dir = tmp_path / "workflow-run"
    run_dir.mkdir()
    lifecycle: list[str] = []

    class FakeSession:
        workflow_run_dir = str(run_dir)
        workflow_run_id = "wf-1"

        async def submit_workflow_stream(self, **_kwargs):
            lifecycle.append("result-produced")
            yield {
                "type": "result",
                "final_outputs": {},
                "error_dict": {},
                "execution_time": 0.1,
            }

        async def writeback_vfs(self):
            lifecycle.append("writeback-complete")

    stream = runner.stream_workflow_job(
        stop=asyncio.Event(),
        workflow={},
        inputs={},
        workflow_run_id="wf-1",
        tenant_id="tenant-1",
        session=FakeSession(),
    )
    message = await anext(stream)

    assert message["type"] == "result"
    assert lifecycle == ["result-produced", "writeback-complete"]

    await stream.aclose()
    assert lifecycle.count("writeback-complete") == 1
