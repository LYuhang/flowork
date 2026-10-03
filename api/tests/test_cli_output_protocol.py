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
        assert cli.main([resource,'publish','--'+resource+'_id',str(uuid4()),'--source_dir',str(source)],socket_path=endpoint) == 0
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
def test_workflow_stream_and_saved_business_result_are_independent(tmp_path,capsys,stream):
    result_file=tmp_path/'result.json'
    with peer(tmp_path,[{'_transport':'heartbeat'},{'status':'running'}, {'terminal':True,'exit_code':0,'status':'succeeded','result':{'outputs':{'answer':42}}}]) as (endpoint, received):
        command=['workflow','run','--workflow_id','wf','--major','v1','--output',str(result_file)]
        assert cli.main(command+(['--stream'] if stream else []),socket_path=endpoint)==0
    output=capsys.readouterr();rows=decoded(output.out)
    assert [x['event'] for x in rows] == (['progress','result'] if stream else ['result'])
    assert rows[-1]['execution_status']=='succeeded'
    assert rows[-1]['command_status']=='succeeded'
    assert json.loads(result_file.read_text()) == {'outputs':{'answer':42}}
    assert 'stream' not in received[0]['arguments']


@pytest.mark.parametrize('resource,action,state,field',[
    ('task','status','failed','execution_status'),
    ('deployment','status','enabled','resource_status'),
    ('deployment','history','failed','execution_status'),
])
def test_successful_query_is_not_business_success(tmp_path,capsys,resource,action,state,field):
    command=[resource,action,'--'+('task_id' if resource=='task' else 'deployment_id'),str(uuid4())]
    if resource=='task':command+=['--task_type','batch_exec']
    if action=='history':command+=['--execution_id',str(uuid4())]
    with peer(tmp_path,[{'status':state}]) as (endpoint,_):
        assert cli.main(command,socket_path=endpoint)==0
    result=json.loads(capsys.readouterr().out)
    assert result['command_status']=='succeeded' and result[field]==state
    assert result['status']==state


def test_task_follow_is_explicit_stream(tmp_path,capsys):
    with peer(tmp_path,[{'_progress':{'status':'running','logs':[]}}, {'status':'failed','execution_error':'Node failed'}]) as (endpoint,_):
        assert cli.main(['task','logs','--task_id',str(uuid4()),'--task_type','batch_exec','--follow'],socket_path=endpoint)==0
    output=capsys.readouterr();rows=decoded(output.out)
    assert [x['event'] for x in rows]==['progress','result'] and not output.err
    assert rows[-1]['command_status']=='succeeded' and rows[-1]['execution_status']=='failed'


def test_unknown_publication_has_one_terminal_error(tmp_path,capsys):
    with peer(tmp_path,[{'_progress':{'status':'publishing'}}]) as (endpoint,_):
        assert cli.main(['knowledge','delete','--knowledge_id',str(uuid4())],socket_path=endpoint)==1
    output=capsys.readouterr();result=json.loads(output.out)
    assert result['event']=='error' and result['command_status']=='unknown'
    assert result['error']=='result_unknown'
    assert decoded(output.err)[0]['event']=='progress'


def test_invalid_arguments_are_one_terminal_json(capsys):
    assert cli.main(['task','not-a-command'])==2
    result=json.loads(capsys.readouterr().out)
    assert result['event']=='error' and result['command_status']=='failed'
