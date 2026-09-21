"""VFS 2b-2b — metadata+exec migration + fusion (unit tests)."""
import json

from vibecanvas_api.storage.vfs_store import _EXT
from vibecanvas_api.agents.tools._envelope import _inline_or_omit


def test_ext_has_html():
    assert _EXT["text/html"] == "html"


def test_inline_or_omit_small_returns_value():
    small = [{"a": 1}]
    assert _inline_or_omit(small) == small


def test_inline_or_omit_large_returns_none():
    big = "x" * 20000  # > 16000 char cap
    assert _inline_or_omit(big) is None


def test_inline_or_omit_dict_under_cap():
    d = {"status": "ok", "node_outputs": {"n": 1}}
    assert _inline_or_omit(d) == d


import asyncio
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text

from vibecanvas_api.storage.vfs_store import PostgresVfsStore
from vibecanvas_api.storage.chat_repo import ChatRepo
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.sync_session import current_sync_tenant_id
from vibecanvas_api.storage.workflow_repo import WorkflowRepo


async def _seed(app_engine, tenant, wf_id, chat_id, user):
    async with app_engine.begin() as c:
        await c.execute(text("INSERT INTO tenants(tenant_id,name) VALUES (:t,'x')"), {"t": tenant})
        await c.execute(text("INSERT INTO users(user_id,tenant_id,email) VALUES (:u,:t,:e)"),
                        {"u": user, "t": tenant, "e": f"{user.hex[:6]}@example.com"})
    async with session_scope(tenant_id=str(tenant)) as session:
        await WorkflowRepo(session, str(user)).create_workflow(
            wf_id=wf_id,
            name="W",
        )
        await ChatRepo(session, str(user)).register_session(
            wf_id,
            name="Chat",
            chat_id=chat_id,
        )


class _Repo:
    def __init__(self, wf): self._wf = wf
    def get_current_workflow(self, wf_id): return self._wf
    def get_meta(self, wf_id): return {"wf_id": "wf_b", "active_major": 1, "active_sub": 0}

class _FakeSession:
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.workflow_run_dir = run_dir
        self.workflow_run_id = "wf_b"
class _Ctx:
    def __init__(self, vfs, wf=None, username="u", session=None, tenant_id=None):
        self.repo = _Repo(wf or {}); self.vfs = vfs
        self.wf_id = "wf_b"; self.chat_id = "chat_b"; self.username = username
        self.workflow = wf or {}
        self.workflow_dirty = False
        self.tenant_id = tenant_id
        self._session = session

    async def sandbox_session(self):
        if self._session is None:
            raise ValueError("no session")
        return self._session
class _Rt:
    def __init__(self, vfs, wf=None, username="u", session=None, tenant_id=None):
        self.context = _Ctx(vfs, wf, username, session, tenant_id)


def _runnable_wf():
    # Minimal workflow the engine can trigger. If StartNode/EndNode need more
    # fields, read core/node.py and add them — the goal is trigger() returns.
    return {
        "node_1": {"node_id": "node_1", "node_type": "StartNode", "node_name": "start",
                   "node_description": "", "input_fields": {}, "output_fields": {},
                   "node_config": {}, "children": ["node_2"], "__attributes__": {}},
        "node_2": {"node_id": "node_2", "node_type": "EndNode", "node_name": "end",
                   "node_description": "", "input_fields": {}, "output_fields": {},
                   "node_config": {}, "children": [], "__attributes__": {}},
        "__meta__": {"workflow_id": "wf_b", "workflow_version": 1, "workflow_subversion": 0},
    }


@pytest.mark.asyncio
async def test_run_workflow_whole_writes_exec_run(app_engine, tmp_path, monkeypatch):
    import importlib
    rw = importlib.import_module("vibecanvas_api.services.platform_mcp.run_tools.run_workflow")
    t, u = uuid.uuid4(), uuid.uuid4()
    await _seed(app_engine, t, "wf_b", "chat_b", u)
    store = PostgresVfsStore()
    session = _FakeSession(str(tmp_path / "run"))
    rt = _Rt(store, wf=_runnable_wf(), username=str(u), session=session, tenant_id=str(t))

    # run_workflow runs in the resident workflow sandbox; mock the shared runner.
    def _fake_run_workflow_once_sync(session, *, tenant_id, workflow, inputs,
                                     workflow_run_id, **kw):
        return SimpleNamespace(result_json={
            "final_outputs": {},
            "error_dict": {},
            "execution_time": 0.0,
        })

    monkeypatch.setattr(rw, "run_workflow_once_sync", _fake_run_workflow_once_sync)

    tok = current_sync_tenant_id.set(str(t))
    try:
        content, artifact = await asyncio.to_thread(rw._sync_run_workflow, "{}", rt)
        data = json.loads(content)
        assert data["status"] == "success"
        assert "exec_id" not in data
        assert artifact["meta"]["content_type"] == "application/json"
    finally:
        current_sync_tenant_id.reset(tok)


@pytest.mark.asyncio
async def test_run_workflow_unknown_node_errors(app_engine):
    import importlib
    rw = importlib.import_module("vibecanvas_api.services.platform_mcp.run_tools.run_workflow")
    t, u = uuid.uuid4(), uuid.uuid4()
    await _seed(app_engine, t, "wf_b", "chat_b", u)
    rt = _Rt(PostgresVfsStore(), wf=_runnable_wf())
    tok = current_sync_tenant_id.set(str(t))
    try:
        _content, artifact = await asyncio.to_thread(rw._sync_run_workflow, "[]", rt)
        assert artifact["status"] == "error"
        assert artifact["error"]["code"] == "bad_inputs"
    finally:
        current_sync_tenant_id.reset(tok)


def test_registry_has_fused_tools_not_retired():
    # Platform workflow execution is not a Runtime-private built-in,
    # while tabular work is handled through bash/Python instead of dedicated
    # tools.
    from vibecanvas_api.agents.tools import builtin_tool_names
    names = builtin_tool_names()
    assert "run_workflow" not in names
    assert names.isdisjoint({
        "inspect_data",
        "write_cells",
        "analyze_data_structure",
        "inspect_sheet",
        "run_node",
        "poll_task",
        "browse_template_market",
        "get_template_detail",
    })
