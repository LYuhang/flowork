import json
from uuid import uuid4

from vibecanvas_api.flowork_cli import cli, local_command


def test_queries_read_same_run_without_host(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(local_command, 'RUN_ROOT', tmp_path)
    monkeypatch.setattr(local_command, 'execution_alive', lambda run_id: True)
    monkeypatch.setattr(cli, 'request', lambda *a, **k: (_ for _ in ()).throw(AssertionError('host called')))
    run_id = uuid4().hex
    directory = tmp_path / run_id
    directory.mkdir()
    output = directory / 'results.jsonl'
    output.write_text('{"index":0,"status":"success","output":{"ok":true}}\n{"index":1')
    status = {'run_id': run_id, 'status': 'waiting_approval', 'path': str(output),
              'approvals': [{'index': 1, 'approval_id': 'a'}]}
    (directory / 'status.json').write_text(json.dumps(status))
    assert cli.main(['workflow', 'status', '--run-id', run_id]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['execution_status'] == 'waiting_approval'
    assert value['approvals'][0]['index'] == 1
    assert cli.main(['workflow', 'result', '--run-id', run_id]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['partial'] and len(value['results']) == 1
    output.write_text(output.read_text().splitlines()[0] + '\n' + '{"index":1,"status":"error"}\n')
    status.update(status='completed_with_errors', total=2, completed=2)
    (directory / 'status.json').write_text(json.dumps(status))
    assert cli.main(['workflow', 'result', '--run-id', run_id, '--limit', '1']) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['next_offset'] == 1 and not value['partial']
    assert value['next_action']['type'] == 'read_next_page'
    assert '--offset 1 --limit 1' in value['pagination']['next_command']
    assert cli.main(['workflow', 'result', '--run-id', run_id, '--index', '1']) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['results'] == [{'index': 1, 'status': 'error'}]
    assert value['command_status'] == 'succeeded'
    assert value['next_action']['type'] == 'analyze_results'
    assert value['next_action']['command'] is None


def test_queries_reject_paths_and_unknown_ids(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(local_command, 'RUN_ROOT', tmp_path)
    assert cli.main(['workflow', 'status', '--run-id', '../status']) == 2
    assert json.loads(capsys.readouterr().out)['error'] == 'invalid_run_id'
    assert cli.main(['workflow', 'status', '--run-id', uuid4().hex]) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'run_not_found'


def test_cli_handoff_keeps_same_worker_alive(tmp_path, monkeypatch, capsys):
    import subprocess
    import sys
    from vibecanvas_api.flowork_cli import local_process
    real_popen = subprocess.Popen
    processes = []
    script = r'''
import json, os, sys, time
payload = json.load(sys.stdin)
channel = os.fdopen(int(sys.argv[1]), 'w')
root = payload['root']
run_id = payload['args']['_run_id']
channel.write(json.dumps({'type':'progress','value':{'run_id':run_id,'status':'waiting_approval','approvals':[{'approval_id':'review'}]}})+'\n')
channel.flush()
while not os.path.exists(root+'/approve'):
    time.sleep(.01)
with open(root+'/finished', 'w') as f:
    f.write(str(os.getpid()))
try:
    channel.write(json.dumps({'type':'result','value':{'status':'completed'},'exit_code':0})+'\n')
    channel.flush()
except BrokenPipeError:
    os._exit(0)
'''
    def spawn(command, **kwargs):
        process = real_popen([sys.executable, '-c', script, command[-1]], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(local_process.subprocess, 'Popen', spawn)
    args = cli.parser().parse_args(['workflow', 'run', '--workflow-id', 'wf', '--major', 'v1'])
    try:
        assert local_process.launch(args, 'unused', cli, tmp_path) == 0
        value = json.loads(capsys.readouterr().out)
        assert value['async'] and value['execution_status'] == 'waiting_approval'
        assert value['run_id'] in value['status_command']
        assert processes[0].poll() is None
        (tmp_path / 'approve').touch()
        assert processes[0].wait(timeout=5) == 0
        assert (tmp_path / 'finished').read_text() == str(processes[0].pid)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_status_distinguishes_dead_worker_and_missing_runtime(tmp_path, monkeypatch, capsys):
    from vibecanvas_api.services.sandbox.local_activity import execution_activity, execution_alive
    run_id = uuid4().hex
    monkeypatch.setattr(local_command, 'RUN_ROOT', tmp_path / 'runs')
    monkeypatch.setattr(local_command, 'execution_alive', lambda identifier: execution_alive(identifier, tmp_path / 'work'))
    directory = local_command.RUN_ROOT / run_id
    directory.mkdir(parents=True)
    state = directory / 'status.json'
    state.write_text(json.dumps({'run_id': run_id, 'status': 'waiting_approval', 'approvals': [{'id': 'a'}]}))
    with execution_activity(tmp_path / 'work', run_id=run_id):
        assert cli.main(['workflow', 'status', '--run-id', run_id]) == 0
        assert json.loads(capsys.readouterr().out)['execution_status'] == 'waiting_approval'
    assert cli.main(['workflow', 'status', '--run-id', run_id]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['execution_status'] == 'interrupted' and value['approvals'] == []
    assert value['last_known_status'] == 'waiting_approval'
    (tmp_path / 'work' / 'local-executions' / run_id).unlink()
    assert cli.main(['workflow', 'status', '--run-id', run_id]) == 0
    assert json.loads(capsys.readouterr().out)['execution_status'] == 'unknown'
    # Reconcile a completion committed during the liveness check.
    def finish(identifier):
        state.write_text(json.dumps({'run_id': run_id, 'status': 'completed', 'approvals': []}))
        return False
    monkeypatch.setattr(local_command, 'execution_alive', finish)
    assert cli.main(['workflow', 'status', '--run-id', run_id]) == 0
    assert json.loads(capsys.readouterr().out)['execution_status'] == 'completed'


def test_worker_lock_releases_after_sigkill(tmp_path):
    import subprocess
    import sys
    from vibecanvas_api.services.sandbox.local_activity import execution_alive
    run_id = uuid4().hex
    script = '''import sys, time
from vibecanvas_api.services.sandbox.local_activity import execution_activity
with execution_activity(sys.argv[1], run_id=sys.argv[2]):
    print('ready', flush=True)
    time.sleep(60)
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(tmp_path), run_id], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'ready'
        assert execution_alive(run_id, tmp_path) is True
        process.kill()
        process.wait(timeout=5)
        assert execution_alive(run_id, tmp_path) is False
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()


def test_status_actions_and_terminal_contract():
    from vibecanvas_api.flowork_cli.local_output import describe
    expected = {
        'preparing': ('check_status_later', False),
        'running': ('check_status_later', False),
        'waiting_approval': ('request_human_approval', False),
        'completed': ('read_results', True),
        'completed_with_errors': ('inspect_errors', True),
        'failed': ('inspect_errors', True),
        'cancelled': ('inspect_errors', True),
        'interrupted': ('inspect_errors', True),
        'unknown': ('verify_runtime', None),
    }
    for state, (action, terminal) in expected.items():
        result = describe({'run_id': 'sample', 'status': state, 'execution_status': 'stale'})
        assert result['execution_status'] == state
        assert result['terminal'] is terminal
        assert result['next_action']['type'] == action
        assert '--run-id sample' in result['next_action']['command']


def test_missing_results_are_query_error_not_execution_failure(tmp_path, monkeypatch, capsys):
    run_id = uuid4().hex
    directory = tmp_path / run_id
    directory.mkdir()
    monkeypatch.setattr(local_command, 'RUN_ROOT', tmp_path)
    (directory / 'status.json').write_text(json.dumps({'run_id': run_id, 'status': 'completed', 'path': str(directory / 'missing.jsonl')}))
    assert cli.main(['workflow', 'result', '--run-id', run_id]) == 1
    value = json.loads(capsys.readouterr().out)
    assert value['command_status'] == 'failed'
    assert value['error'] == 'execution_evidence_unavailable'
    assert 'execution_status' not in value
    (directory / 'status.json').write_text('{broken')
    assert cli.main(['workflow', 'status', '--run-id', run_id]) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'execution_evidence_unavailable'


def test_terminal_partial_results_do_not_imply_more_work_is_running(tmp_path, monkeypatch, capsys):
    run_id = uuid4().hex
    directory = tmp_path / run_id
    directory.mkdir()
    monkeypatch.setattr(local_command, 'RUN_ROOT', tmp_path)
    output = directory / 'results.jsonl'
    output.write_text('{"index":2,"status":"success","output":{}}\n')
    summary = {'run_id': run_id, 'status': 'cancelled', 'path': str(output), 'total': 3, 'completed': 1}
    (directory / 'status.json').write_text(json.dumps(summary))
    assert cli.main(['workflow', 'result', '--run-id', run_id]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['terminal'] is True and value['partial'] is True
    assert value['results_complete'] is False
    assert value['pagination']['next_command'] is None
    assert value['next_action']['type'] == 'analyze_results'
    assert cli.main(['workflow', 'result', '--run-id', run_id, '--index', '0']) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['results'] == [] and value['result_available'] is False
    assert value['execution_status'] == 'cancelled'
