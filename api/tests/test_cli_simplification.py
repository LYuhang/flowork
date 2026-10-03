"""Canonical options, removed commands, and durable evaluation observations."""
import argparse
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from vibecanvas_api.flowork_cli import cli
from vibecanvas_api.services import batch_evaluation as evaluation
from vibecanvas_api.services.agent_runtime import cli_tasks


def test_all_registered_options_use_hyphens_without_aliases():
    def check(parser):
        for action in parser._actions:
            options = [x for x in action.option_strings if x.startswith('--')]
            assert all('_' not in x and x not in {'--inputs', '--inputs-file'} for x in options)
            assert len(options) <= 1
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    check(child)
    check(cli.parser())


@pytest.mark.parametrize('argv', [
    ['task','list','--query','test'], ['skill','list','--search','test'],
    ['knowledge','list','--search','test'], ['mcp','list','--search','test'],
    ['workflow','list','--search','test'], ['deployment','list','--search','test'],
    ['workflow','run','--workflow_id','test','--major','v1'],
    ['workflow','run','--workflow-id','test','--major','v1','--inputs','{}'],
    ['task','create','--task-type','schedule_run','--workflow-id','test','--version','v1.sv0','--interval','30','--inputs_file','test.json'],
])
def test_removed_options_never_dispatch(argv, monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(cli, 'request', request)
    assert cli.main(argv, socket_path='test') == 2
    request.assert_not_called()


@pytest.mark.parametrize('state', ['queued','running','succeeded','failed'])
@pytest.mark.asyncio
async def test_logs_wait_for_evaluation_after_inference_finishes(state, monkeypatch):
    @asynccontextmanager
    async def session(**kwargs): yield object()
    monkeypatch.setattr(cli_tasks, 'session_scope', session)
    monkeypatch.setattr(cli_tasks, 'resource_route_params', lambda *args: {})
    task_id = str(uuid4())
    task = {'id': task_id, 'task_type': 'batch_exec', 'status': 'finished', 'result': {'answer': 42},
            'payload': {'evaluation': {'enabled': True}, 'evaluations': [{'status': state}]}}
    monkeypatch.setattr(cli_tasks.routes, 'get_task', AsyncMock(return_value=task))
    monkeypatch.setattr(cli_tasks.routes, 'list_task_events', AsyncMock(return_value={'items': [], 'next_cursor': None}))
    result = await cli_tasks._read(SimpleNamespace(tenant_id='t'), 'task.logs',
        {'task_id': task_id, 'task_type': 'batch_exec'}, AsyncMock())
    assert result['terminal'] == (state in {'succeeded','failed'})
    assert result['evaluation_status'] == state
    assert result['status'] == 'finished' and result['result'] == {'answer': 42}


@pytest.mark.parametrize('error', [None, 'bad evaluation'])
@pytest.mark.asyncio
async def test_evaluation_completion_atomically_persists_metrics_log(error, monkeypatch):
    from vibecanvas_api.storage import sync_session
    from vibecanvas_api.services.sandbox import coordinator
    task_id, evaluation_id = uuid4(), str(uuid4())
    record = {'id': evaluation_id, 'status': 'queued', 'script': 'def evaluate(results): return {}', 'result_version': 'v', 'row_count': 1}
    task = SimpleNamespace(id=task_id, tenant_id=uuid4(), user_id=uuid4(), status='finished',
        payload={'evaluations': [record]}, result={'answer': 42})
    repo = SimpleNamespace(get=AsyncMock(return_value=task), update_status=AsyncMock(), insert_event=AsyncMock())
    @asynccontextmanager
    async def session(): yield object()
    monkeypatch.setattr(sync_session, 'short_admin_session', session)
    monkeypatch.setattr(evaluation, 'TasksRepo', lambda session: repo)
    monkeypatch.setattr(evaluation, 'result_uri', lambda task: 'uri')
    monkeypatch.setattr(evaluation, 'load_results', lambda uri: ([{'x': 1}], 'v'))
    sandbox = SimpleNamespace(run_code=AsyncMock(return_value={'exit_code': 0, 'stdout': json.dumps({'metrics': {'count': 1}, 'error': error})}))
    service = SimpleNamespace(get_session=AsyncMock(return_value=sandbox), close_session=AsyncMock())
    monkeypatch.setattr(coordinator, 'get_sandbox_coordinator', lambda: service)
    monkeypatch.setattr(coordinator, 'dispose_sandbox_rpc_client', AsyncMock())
    await evaluation.run_evaluation(str(task_id), evaluation_id)
    event = repo.insert_event.await_args.args[2]
    assert event['action'] == ('evaluation.failed' if error else 'evaluation.succeeded')
    assert event['data']['metrics'] == (None if error else {'count': 1})
    assert event['data']['error'] == error
    assert 'script' not in event['data']
    assert all('status' not in call.kwargs for call in repo.update_status.await_args_list)
    assert task.result == {'answer': 42}


def test_guidance_describes_current_contract_only(capsys):
    from vibecanvas_api.services.agent_runtime.cli_gateway import platform_guidance
    text = platform_guidance()
    for phrase in ['--inputs', 'evaluation-config', 'compatibility alias', 'old status', 'legacy README']:
        assert phrase not in text
    assert '--input-file' in text and '--evaluation-script' in text
    with pytest.raises(SystemExit):
        cli.parser().parse_args(['--help'])
    assert 'legacy' not in capsys.readouterr().out.lower()
