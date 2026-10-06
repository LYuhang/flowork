import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def planner():
    path = Path.cwd() / 'scripts/security/plan_posix_workspace_migration.py'
    spec = importlib.util.spec_from_file_location('migration_plan', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('current', [False, True])
def test_differing_run_snapshots_require_authoritative_current_run(tmp_path, monkeypatch, planner, current):
    from vibecanvas_api.services.object_store import InMemoryObjectStore
    store = InMemoryObjectStore()
    store.put_bytes('older', b'old')
    store.put_bytes('current', b'new')
    monkeypatch.setattr(planner, 'get_object_store', lambda: store)
    rows = [dict(category='run', status='run_destination_collision', tenant_id='tenant', kind='workflow',
                 resource_id='workflow', relative_path='counter', source_key=key,
                 current_workflow_run=current and key == 'current') for key in ['older', 'current']]
    inventory = tmp_path / 'inventory.jsonl'
    inventory.write_text('\n'.join(json.dumps(r) for r in rows))
    result = planner.plan(inventory)
    if current:
        assert result['summary']['conflicts'] == 0
        assert result['files'][0]['source']['source_key'] == 'current'
        assert result['retained'][0]['source_key'] == 'older'
        assert result['resolutions'][0]['reason'] == 'current_workflow_run_state'
    else:
        assert result['summary']['conflicts'] == 1
        assert result['files'] == []
