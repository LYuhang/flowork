import base64
import json
from uuid import uuid4

from vibecanvas_api.flowork_cli import cli, skill_cli


def test_local_template_does_not_publish_or_overwrite(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, 'request', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('unexpected request')))
    target = tmp_path / 'new-skill'
    args = ['skill','init','--name','my-skill','--output_dir',str(target)]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)['published'] is False
    original = (target/'SKILL.md').read_text()
    assert 'name: my-skill' in original
    assert cli.main(args) == 2
    assert (target/'SKILL.md').read_text() == original


def test_skill_update_freezes_entire_package_and_is_a_write(tmp_path, monkeypatch, capsys):
    (tmp_path/'SKILL.md').write_text('---\nname: example\ndescription: Example\n---\nInstructions')
    (tmp_path/'reference.txt').write_text('details')
    calls = []
    monkeypatch.setattr(cli,'request',lambda endpoint,args,**kwargs: calls.append((args,kwargs)) or {'version':2})
    identifier = str(uuid4())
    assert cli.main(['skill','update','--skill_id',identifier,'--source_dir',str(tmp_path)],socket_path='test') == 0
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
