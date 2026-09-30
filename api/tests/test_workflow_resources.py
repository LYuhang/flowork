from concurrent.futures import ThreadPoolExecutor
import json
import os
import shutil
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from vibecanvas_api.services import workflow_resources as resources


def graph(skill_id):
    return {"worker": {"node_type": "SubAgentNode", "node_config": {
        "skills": [{"id": skill_id, "name": "Old display name"}], "mcp_servers": []}}}


def test_immutable_publication_preserves_mount_and_concurrent_versions(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    inode = root.stat().st_ino
    identifier = str(uuid4())
    def publish(letter):
        return resources.publish_skill_files(str(root), skill_id=identifier, revision_hash=letter * 64,
            files=[("SKILL.md", "text/markdown", letter.encode()), ("scripts/a.py", "text/plain", b"print(1)")])
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(publish, ["a", "a", "b"]))
    assert root.stat().st_ino == inode
    assert (root / identifier / ("a" * 64) / "SKILL.md").read_text() == "a"
    assert (root / identifier / ("b" * 64) / "SKILL.md").read_text() == "b"
    assert not list(tmp_path.glob('.skill-publish-*'))


@pytest.mark.parametrize("path", ["../escape", "/absolute", "folder/../../escape", "folder\\escape"])
def test_invalid_skill_file_never_publishes(tmp_path, path):
    root = tmp_path / "skills"
    identifier = str(uuid4())
    with pytest.raises(ValueError):
        resources.publish_skill_files(str(root), skill_id=identifier, revision_hash="a" * 64,
            files=[("SKILL.md", "text/plain", b"safe"), (path, "text/plain", b"bad")])
    assert not (root / identifier / ("a" * 64)).exists()
    assert not list(tmp_path.glob('.skill-publish-*'))


@pytest.mark.asyncio
async def test_latest_resolution_is_per_run_and_does_not_rewrite_saved_graph(monkeypatch):
    identifier = str(uuid4())
    workflow = graph(identifier)
    rows = [[{"skill_id": identifier, "name": "Current name", "description": "Audit",
              "revision_hash": letter * 64, "current_revision_id": str(uuid4())}] for letter in ("a", "b")]
    repo = SimpleNamespace(list_authorized=AsyncMock(side_effect=rows))
    monkeypatch.setattr(resources, "SkillsRepo", lambda session: repo)
    service = SimpleNamespace(list_authorized_ids=AsyncMock(return_value=[identifier]),
                              check=AsyncMock(return_value=SimpleNamespace(allowed=True)))
    args = dict(session=object(), workflow=workflow, service=service, principal=object(),
                context=SimpleNamespace(active_organization_id="tenant"))
    first = await resources.resolve_workflow_resources(**args)
    second = await resources.resolve_workflow_resources(**args)
    assert first["nodes"]["worker"]["skills"][0]["revision_hash"] == "a" * 64
    assert second["nodes"]["worker"]["skills"][0]["revision_hash"] == "b" * 64
    assert workflow == graph(identifier)
    service.check.return_value = SimpleNamespace(allowed=False)
    repo.list_authorized.side_effect = None
    repo.list_authorized.return_value = rows[1]
    with pytest.raises(PermissionError):
        await resources.resolve_workflow_resources(**args)


def test_resource_references_reject_versions_and_duplicates():
    workflow = graph(str(uuid4()))
    refs = workflow["worker"]["node_config"]["skills"]
    refs[0]["revision_hash"] = "a" * 64
    with pytest.raises(ValueError, match="id/name"):
        resources.collect_subagent_resources(workflow)
    refs[0].pop("revision_hash")
    refs.append({**refs[0], "name": "Alias"})
    with pytest.raises(ValueError, match="duplicate"):
        resources.collect_subagent_resources(workflow)


@pytest.mark.skipif(os.environ.get("FLOWORK_TEST_SKILL_MOUNT") != "1" or not shutil.which("bwrap"),
                    reason="explicit native Bubblewrap Skill mount check")
def test_live_sandbox_sees_new_skill_without_restart_but_cannot_write(tmp_path, monkeypatch):
    from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider

    root, work, runs = (tmp_path / name for name in ("skills", "work", "runs"))
    for folder in (root, work, runs):
        folder.mkdir()
    identifier = str(uuid4())
    resources.publish_skill_files(str(root), skill_id=identifier, revision_hash="a" * 64,
                                 files=[("SKILL.md", "text/plain", b"first")])
    script = '''
import json,time
from pathlib import Path
root=Path('/skills')
first=list(root.glob('*/*/SKILL.md'))[0]
readonly=False
try: first.write_text('changed')
except OSError: readonly=True
Path('/work/initial.json').write_text(json.dumps({'readonly':readonly,'first':first.read_text()}))
until=time.monotonic()+15
while time.monotonic()<until:
    contents=sorted(p.read_text() for p in root.glob('*/*/SKILL.md'))
    if len(contents)==2:
        Path('/work/updated.json').write_text(json.dumps(contents))
        break
    time.sleep(.05)
until=time.monotonic()+15
while time.monotonic()<until:
    if not list(root.glob('*/*/SKILL.md')):
        Path('/work/revoked.json').write_text(json.dumps({'removed': True}))
        break
    time.sleep(.05)
'''
    provider = BubblewrapProvider(shutil.which("bwrap"))
    handle = provider.run_serve(runs_root=str(runs), work_dir=str(work),
        extra_ro_dest_binds=[("/skills", str(root))], command=["/usr/bin/python3", "-c", script])
    def wait_file(path):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if path.exists():
                return json.loads(path.read_text())
            if handle.proc.poll() is not None:
                raise AssertionError(handle.proc.communicate()[1])
            time.sleep(.05)
        raise AssertionError("sandbox did not publish mount verification")
    try:
        assert wait_file(work / "initial.json") == {"readonly": True, "first": "first"}
        resources.publish_skill_files(str(root), skill_id=identifier, revision_hash="b" * 64,
                                     files=[("SKILL.md", "text/plain", b"second")])
        assert wait_file(work / "updated.json") == ["first", "second"]
        import asyncio
        from vibecanvas_api.services import workflow_skill_cache as cache
        monkeypatch.setattr(cache, '_workflow_execution_is_active', AsyncMock(return_value=True))
        asyncio.run(cache.reconcile_skill_cache(session=object(), root=str(root), snapshot={
            'execution': {'execution_id': 'native-running'}, 'lease_id': 'native',
            'skills': [{'id': identifier, 'revision_hash': letter * 64} for letter in ('a', 'b')]}))
        inode = root.stat().st_ino
        asyncio.run(cache.reconcile_skill_cache(session=object(), root=str(root),
            authorize=AsyncMock(return_value=[])))
        assert wait_file(work / "revoked.json") == {'removed': True}
        assert root.stat().st_ino == inode
    finally:
        provider.stop_serve(handle)


@pytest.mark.asyncio
async def test_save_refreshes_authorized_names_without_mutating_or_disclosing(monkeypatch):
    identifier = str(uuid4())
    workflow = graph(identifier)
    repo = SimpleNamespace(get=AsyncMock(return_value={'name': 'Renamed Skill'}))
    monkeypatch.setattr(resources, 'SkillsRepo', lambda session: repo)
    service = SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(allowed=True)))
    kwargs = dict(session=object(), workflow=workflow, service=service,
        principal=object(), context=SimpleNamespace(active_organization_id='tenant'))
    saved = await resources.canonicalize_resource_names(**kwargs)
    assert saved['worker']['node_config']['skills'] == [{'id': identifier, 'name': 'Renamed Skill'}]
    assert workflow == graph(identifier)
    service.check.return_value = SimpleNamespace(allowed=False)
    repo.get.reset_mock()
    assert await resources.canonicalize_resource_names(**kwargs) == workflow
    repo.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,schema,expected', [
    ('failed', {'type': 'object'}, 'workflow_mcp_unavailable'),
    ('connected', {'type': 'invalid-type'}, 'workflow_mcp_invalid_tool_schema'),
    ('not_required', {'type': 'object'}, None),
])
async def test_mcp_preparation_rejects_unusable_resources_before_node_execution(monkeypatch, status, schema, expected):
    identifier = str(uuid4())
    workflow = {'worker': {'node_type': 'SubAgentNode', 'node_config': {
        'mcp_servers': [{'id': identifier, 'name': 'calculator'}]}}}
    repo = SimpleNamespace(get=AsyncMock(return_value={
        'name': 'calculator', 'enabled': True, 'connection_status': status,
        'last_tool_names': [{'name': 'add', 'input_schema': schema}]}))
    monkeypatch.setattr(resources, 'McpServersRepo', lambda session: repo)
    service = SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(allowed=True)))
    args = dict(session=object(), workflow=workflow, service=service, principal=object(),
                context=SimpleNamespace(active_organization_id='tenant'))
    if expected:
        with pytest.raises((PermissionError, ValueError), match=expected):
            await resources.resolve_workflow_resources(**args)
    else:
        result = await resources.resolve_workflow_resources(**args)
        assert result['mcp_servers'][0]['tools'][0]['name'] == 'add'
