import base64
import json

import pytest
from uuid import uuid4

from vibecanvas_api.flowork_cli import cli, skill_cli


@pytest.mark.parametrize('arguments', [
    ['files', '--skill_id', str(uuid4())],
    ['read', '--skill_id', str(uuid4())],
    ['init', '--name', 'example', '--output_dir', '/tmp/unused'],
    ['update', '--skill_id', str(uuid4())],
    ['refresh', '--skill_id', str(uuid4()), '--source_dir', '/tmp/unused'],
])
def test_removed_commands_never_dispatch(arguments, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'request', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('unexpected request')))
    assert cli.main(['skill', *arguments], socket_path='test') == 2
    capsys.readouterr()


def test_skill_publish_freezes_entire_package_and_is_a_write(tmp_path, monkeypatch, capsys):
    (tmp_path/'SKILL.md').write_text('---\nname: example\ndescription: Example\n---\nInstructions')
    (tmp_path/'reference.txt').write_text('details')
    calls = []
    monkeypatch.setattr(cli,'request',lambda endpoint,args,**kwargs: calls.append((args,kwargs)) or {'version':2})
    identifier = str(uuid4())
    assert cli.main(['skill','publish','--skill_id',identifier,'--source_dir',str(tmp_path)],socket_path='test') == 0
    args, operation = calls[0]
    assert operation['operation'] == 'skill.update'
    assert {item['path'] for item in args['files']} == {'SKILL.md','reference.txt'}
    assert args['skill_id'] == identifier
    assert skill_cli.WRITE_OPERATIONS <= cli.WRITE_OPERATIONS
    assert not skill_cli.WRITE_OPERATIONS & cli.READ_OPERATIONS
    capsys.readouterr()


def test_skill_download_materializes_files_without_overwriting(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli,'request',lambda *args,**kwargs:{'files':[{'path':'SKILL.md','data':base64.b64encode(b'hello').decode()}]})
    target = tmp_path/'download'
    args = ['skill','download','--skill_id',str(uuid4()),'--output_dir',str(target)]
    assert cli.main(args,socket_path='test') == 0
    result = json.loads(capsys.readouterr().out)
    assert 'files' not in result and result['entrypoint'] == str(target/'SKILL.md')
    assert (target/'SKILL.md').read_text() == 'hello'
    assert cli.main(args,socket_path='test') == 2


def test_symlink_package_is_rejected_before_dispatch(tmp_path, monkeypatch, capsys):
    (tmp_path/'SKILL.md').symlink_to('/etc/passwd')
    monkeypatch.setattr(cli,'request',lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('unexpected request')))
    assert cli.main(['skill','check','--source_dir',str(tmp_path)],socket_path='test') == 2
    assert json.loads(capsys.readouterr().out)['error'] == 'invalid_arguments'


def test_delete_dispatches_only_identifier_as_write(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, 'request', lambda endpoint, args, **kw: calls.append((args, kw)) or {'deleted': True})
    identifier = str(uuid4())
    assert cli.main(['skill', 'delete', '--skill_id', identifier], socket_path='test') == 0
    assert calls == [({'skill_id': identifier}, {'operation': 'skill.delete'})]
    assert 'skill.delete' in cli.WRITE_OPERATIONS
    assert json.loads(capsys.readouterr().out)['deleted']


def test_refresh_updates_runtime_only(monkeypatch, capsys):
    calls=[]
    monkeypatch.setattr(cli,'request',lambda endpoint,args,**kwargs:calls.append((args,kwargs)) or {'published':False,'version':3})
    identifier=str(uuid4())
    assert cli.main(['skill','refresh','--skill_id',identifier],socket_path='test') == 0
    assert calls == [({'skill_id':identifier},{'operation':'skill.refresh'})]
    assert 'skill.refresh' in cli.READ_OPERATIONS
    assert 'skill.refresh' not in cli.WRITE_OPERATIONS
    assert json.loads(capsys.readouterr().out)['published'] is False


def test_publish_command_is_explicit_platform_write(tmp_path,monkeypatch,capsys):
    (tmp_path/'SKILL.md').write_text('---\nname: example\ndescription: Example\n---\nInstructions')
    calls=[]
    monkeypatch.setattr(cli,'request',lambda endpoint,args,**kwargs:calls.append(kwargs['operation']) or {'version':2})
    assert cli.main(['skill','publish','--skill_id',str(uuid4()),'--source_dir',str(tmp_path)],socket_path='test') == 0
    assert calls == ['skill.update']
    capsys.readouterr()
