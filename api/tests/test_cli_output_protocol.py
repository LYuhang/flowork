"""Exercise actual socket framing and stdout/stderr, not mocked print calls."""
from contextlib import contextmanager
import json
import socket
import threading
from uuid import uuid4

import pytest

from vibecanvas_api.flowork_cli import cli


@contextmanager
def peer(tmp_path, frames):
    endpoint = str(tmp_path / 'cli.sock')
    received = []
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(endpoint)
        server.listen(1)
        server.settimeout(5)
        def serve():
            with server.accept()[0] as connection:
                received.append(json.loads(connection.makefile('rb').readline()))
                for frame in frames:
                    connection.sendall(json.dumps(frame).encode() + b'\n')
        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield endpoint, received
        finally:
            worker.join(timeout=6)
            assert not worker.is_alive()


def decoded(text):
    return [json.loads(line) for line in text.splitlines()]


@pytest.mark.parametrize('resource', ['knowledge', 'skill'])
def test_publication_stdout_is_one_json_and_approval_progress_is_stderr(tmp_path, capsys, resource):
    source = tmp_path / 'source'
    source.mkdir()
    (source / ('README.md' if resource == 'knowledge' else 'SKILL.md')).write_text('# Test')
    with peer(tmp_path, [{'_transport':'heartbeat'}, {'_progress':{'status':'auto_approved'}},
                         {'_progress':{'status':'publishing'}}, {'status':'succeeded','package_version':2}]) as (endpoint, _):
        assert cli.main([resource,'publish','--'+resource+'-id',str(uuid4()),'--source-dir',str(source),'--expected-version','1'],socket_path=endpoint) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)['command_status'] == 'succeeded'
    assert json.loads(output.out)['event'] == 'result'
    assert [x['status'] for x in decoded(output.err)] == ['auto_approved','publishing']
    assert all(x['event']=='progress' for x in decoded(output.err))


@pytest.mark.parametrize('resource', ['document','diagram'])
@pytest.mark.parametrize('stream', [False,True])
def test_render_opt_in_stream_has_tagged_events(tmp_path,capsys,resource,stream):
    with peer(tmp_path,[{'_progress':{'page':1}}, {'status':'succeeded','complete':True}]) as (endpoint, received):
        command=[resource,'render','--file',str(tmp_path/('source.pdf' if resource=='document' else 'source.drawio'))]
        assert cli.main(command+(['--stream'] if stream else []),socket_path=endpoint) == 0
    output=capsys.readouterr()
    assert [x['event'] for x in decoded(output.out)] == (['progress','result'] if stream else ['result'])
    assert bool(output.err) is not stream
    assert 'stream' not in received[0]['arguments']


@pytest.mark.parametrize('stream', [False,True])
def test_workflow_stream_and_saved_business_result_are_independent(tmp_path, capsys, monkeypatch, stream):
    from vibecanvas_api.flowork_cli import local_command, local_workflow
    from vibecanvas_api.services.sandbox.local_activity import execution_activity
    monkeypatch.setattr(local_command, 'execution_activity', lambda **kw: execution_activity(tmp_path / 'work', **kw))
    monkeypatch.setattr(local_command, 'RUN_ROOT', tmp_path / 'runs')
    monkeypatch.setattr(local_command, 'execute', local_command.execute_in_process)
    result_file = tmp_path / 'result.jsonl'
    prepared = {'workflow': {}, 'context': {}, 'id': 'wf', 'version': 'v1.sv0', 'source': 'saved'}
    async def run_rows(**kwargs):
        kwargs['output'].write(json.dumps({'index': 0, 'status': 'success', 'output': {'answer': 42}}) + '\n')
        kwargs['status']({'status': 'completed', 'completed': 1, 'failed': 0})
        return 0
    monkeypatch.setattr(local_workflow, 'run_rows', run_rows)
    with peer(tmp_path, [prepared]) as (endpoint, received):
        command = ['workflow', 'run', '--workflow-id', 'wf', '--major', 'v1', '--output', str(result_file)]
        assert cli.main(command + (['--stream'] if stream else []), socket_path=endpoint) == 0
    output = capsys.readouterr()
    rows = decoded(output.out)
    assert rows[-1]['event'] == 'result'
    assert all(row['event'] == 'progress' for row in rows[:-1])
    assert (len(rows) > 1) is stream
    assert bool(output.err) is not stream
    assert rows[-1]['execution_status'] == 'completed'
    assert rows[-1]['command_status'] == 'succeeded'
    assert json.loads(result_file.read_text())['output'] == {'answer': 42}
    assert received[0]['operation'] == 'workflow.prepare'
    assert 'stream' not in received[0]['arguments']


@pytest.mark.parametrize('resource,action,state,field',[
    ('task','status','failed','execution_status'),
    ('deployment','info','enabled','resource_status'),
    ('deployment','status','failed','execution_status'),
])
def test_successful_query_is_not_business_success(tmp_path,capsys,resource,action,state,field):
    command=[resource,action,'--'+('task-id' if resource=='task' else 'deployment-id'),str(uuid4())]
    if resource=='task':command+=['--task-type','batch_exec']
    if resource=='deployment' and action=='status':command+=['--execution-id',str(uuid4())]
    with peer(tmp_path,[{'status':state}]) as (endpoint,_):
        assert cli.main(command,socket_path=endpoint)==0
    result=json.loads(capsys.readouterr().out)
    assert result['command_status']=='succeeded' and result[field]==state
    assert result['status']==state


def test_task_follow_is_explicit_stream(tmp_path,capsys):
    with peer(tmp_path,[{'_progress':{'status':'running','logs':[]}}, {'status':'failed','execution_error':'Node failed'}]) as (endpoint,_):
        assert cli.main(['task','logs','--task-id',str(uuid4()),'--task-type','batch_exec','--follow'],socket_path=endpoint)==0
    output=capsys.readouterr();rows=decoded(output.out)
    assert [x['event'] for x in rows]==['progress','result'] and not output.err
    assert rows[-1]['command_status']=='succeeded' and rows[-1]['execution_status']=='failed'


def test_unknown_publication_has_one_terminal_error(tmp_path,capsys):
    with peer(tmp_path,[{'_progress':{'status':'publishing'}}]) as (endpoint,_):
        assert cli.main(['knowledge','delete','--knowledge-id',str(uuid4())],socket_path=endpoint)==1
    output=capsys.readouterr();result=json.loads(output.out)
    assert result['event']=='error' and result['command_status']=='unknown'
    assert result['error']=='result_unknown'
    assert decoded(output.err)[0]['event']=='progress'


def test_invalid_arguments_are_one_terminal_json(capsys):
    assert cli.main(['task','not-a-command'])==2
    result=json.loads(capsys.readouterr().out)
    assert result['event']=='error' and result['command_status']=='failed'


def test_real_local_worker_sync_execution_and_query(tmp_path, monkeypatch, capsys):
    from pathlib import Path
    import vibecanvas_engine
    from vibecanvas_api.flowork_cli import local_command
    monkeypatch.setattr(local_command, 'RUN_ROOT', tmp_path / 'runs')
    graph_file = Path(vibecanvas_engine.__file__).resolve().parents[2] / 'tests' / 'example_workflow.json'
    prepared = {'workflow': json.loads(graph_file.read_text()), 'context': {},
                'id': 'wf', 'version': 'v1.sv0', 'source': 'saved'}
    with peer(tmp_path, [prepared]) as (endpoint, received):
        assert cli.main(['workflow', 'run', '--workflow-id', 'wf', '--major', 'v1',
                         '--input', '{"text":"hello","count":2}'], socket_path=endpoint) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['execution_status'] == 'completed' and result['async'] is False
    assert result['progress']['succeeded'] == 1
    assert cli.main(['workflow', 'result', '--run-id', result['run_id']]) == 0
    queried = json.loads(capsys.readouterr().out)
    assert queried['results_complete'] is True
    assert queried['results'][0]['output']['repeated'] == 'hello hello'
    assert queried['results'][0]['execution_id']
    assert len(received) == 1
