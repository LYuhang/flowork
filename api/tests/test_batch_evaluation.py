"""Evaluation contract, typed result queries and CLI script transport."""
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from vibecanvas_api.services import batch_evaluation as evaluation
from vibecanvas_api.routes import tasks
from vibecanvas_api.flowork_cli import cli, task_cli


@pytest.mark.parametrize('script', ['', 'def evaluate(:', 'import numpy\ndef evaluate(results): return {}', 'x = 1'])
def test_invalid_scripts_are_rejected(script):
    with pytest.raises(ValueError):
        evaluation.validate_script(script)


@pytest.mark.parametrize(('script', 'expected', 'error'), [
    ('import statistics\ndef evaluate(results): return {"mean": statistics.mean(r["input"]["x"] for r in results), "false": False}', {'mean': 2, 'false': False}, None),
    ('def evaluate(results):\n print("noise")\n return {"count": len(results)}', {'count': 2}, None),
    ('def evaluate(results): return []', None, 'dictionary'),
    ('def evaluate(results): return {"bad": float("nan")}', None, 'JSON'),
    ('def evaluate(results): return __import__("numpy")', None, 'approved'),
    ('def evaluate(results): raise ValueError("bad sample")', None, 'bad sample'),
])
def test_evaluator_protocol(script, expected, error):
    # Trusted test fixtures only; actual product runs in its OS sandbox.
    result = subprocess.run([sys.executable, '-c', evaluation.EVALUATOR], input=json.dumps({
        'script': script, 'results': [{'input': {'x': 1}}, {'input': {'x': 3}}],
        'allowed_modules': sorted(evaluation.ALLOWED_MODULES),
    }), text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout)
    if error:
        assert error in answer['error']
    else:
        assert answer['metrics'] == expected


@pytest.mark.asyncio
async def test_queries_filter_whole_dataset_and_keep_typed_values(monkeypatch):
    rows = [{'index': i, 'status': 'success', 'input': {'value': i}, 'output': {'ok': False}} for i in range(250)]
    rows[240]['execution_id'] = str(uuid4())
    monkeypatch.setattr(tasks, '_authorize_task', AsyncMock())
    monkeypatch.setattr(tasks, 'TasksRepo', lambda session: SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
        task_type='batch_exec', status='finished', result={'artifact_uris': {'jsonl': 'memory://test'}}))))
    from vibecanvas_api.services import result_tables
    from vibecanvas_api.services.object_store import InMemoryObjectStore
    store = InMemoryObjectStore()
    store.put_bytes('test', '\n'.join(json.dumps(row) for row in rows).encode())
    monkeypatch.setattr(result_tables, 'get_task_result_store', lambda: store)
    result = await tasks.query_results(uuid4(), tasks.ResultQuery(search='240', limit=10), None, SimpleNamespace(tenant_id=uuid4(), user_id=uuid4()), None, None)
    assert result['total'] == 250
    assert result['rows'][0]['input']['value'] == 240
    assert result['rows'][0]['output']['ok'] is False
    assert result['rows'][0]['execution_id'] == rows[240]['execution_id']


def test_live_results_not_readable_and_missing_artifact():
    for status, code in [('running', 409), ('resuming', 409), ('finished', 404)]:
        with pytest.raises(HTTPException) as exc:
            evaluation.result_uri(SimpleNamespace(task_type='batch_exec', status=status, result={}))
        assert exc.value.status_code == code


def test_cli_reads_script_content_not_path(tmp_path, monkeypatch):
    script = tmp_path/'evaluate.py'
    script.write_text('def evaluate(results): return {"total": len(results)}')
    rows = tmp_path/'rows.jsonl'
    rows.write_text('{"value": 1}\n')
    seen = []
    monkeypatch.setattr(cli, 'request', lambda endpoint, arguments, **kw: seen.append(arguments) or {'status': 'queued'})
    assert cli.main(['task', 'create', '--task-type', 'batch_exec', '--workflow-id', 'wf', '--version', 'v1.sv0', '--input-file', str(rows), '--evaluation-script', str(script)], socket_path='test') == 0
    assert seen[-1]['evaluation_script'] == script.read_text()



@pytest.mark.parametrize("action", ["evaluation", "evaluate", "evaluation-config"])
def test_removed_cli_evaluation_commands_are_rejected(action, monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(cli, "request", request)
    assert cli.main(["task", action, "--task-type", "batch_exec", "--task-id", str(uuid4())], socket_path="test") == 2
    request.assert_not_called()
    with pytest.raises(ValueError):
        task_cli.validate("task." + action, {"task_type": "batch_exec", "task_id": str(uuid4())})
