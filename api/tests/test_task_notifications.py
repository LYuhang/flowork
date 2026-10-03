import pytest
from pydantic import ValidationError
from vibecanvas_api.routes.tasks import ScheduledRunCreateBody, ScheduledRunPatchBody
from vibecanvas_api.routes.workflows import BatchSubmitBody
from vibecanvas_api.services.task_notifications import notification_state

@pytest.mark.parametrize('events', [[], ['succeeded'], ['failed'], ['succeeded', 'failed']])
def test_shared_notification_contract(events):
    policy = {'enabled': bool(events), 'on': events, 'email': ' test@example.com '}
    bodies = [BatchSubmitBody(data_source={}, column_mapping={}, notification_policy=policy),
              ScheduledRunCreateBody(name='Task', workflow_id='wf', notification_policy=policy),
              ScheduledRunPatchBody(notification_policy=policy)]
    for body in bodies:
        assert body.notification_policy == {'enabled': bool(events), 'on': events, 'email': 'test@example.com' if events else ''}

@pytest.mark.parametrize('email', ['', 'invalid', 'a@b', 'one@example.com,two@example.com'])
def test_invalid_recipient_rejected(email):
    with pytest.raises(ValidationError):
        BatchSubmitBody(data_source={}, column_mapping={}, notification_policy={'on': ['failed'], 'email': email})

def test_placeholder_never_claims_queued_or_sent():
    assert notification_state({'enabled': True, 'on': ['succeeded'], 'email': 'test@example.com'}, 'succeeded') == {'status': 'skipped', 'reason': 'delivery_not_implemented'}
    assert notification_state({'enabled': True, 'on': ['failed']}, 'succeeded')['reason'] == 'policy_not_matched'

@pytest.mark.parametrize('task_type', ['batch_exec', 'schedule_run'])
def test_cli_notification_flags(task_type, tmp_path, monkeypatch):
    from vibecanvas_api.flowork_cli import cli
    from vibecanvas_api.services.agent_runtime import cli_tasks
    table = tmp_path / 'rows.csv'
    table.write_text('value\n1\n')
    seen = []
    monkeypatch.setattr(cli, 'request', lambda endpoint, arguments, **kw: seen.append(arguments) or {'status': 'queued'})
    args = ['task', 'create', '--task-type', task_type, '--workflow-id', 'wf', '--version', 'v1.sv0']
    args += ['--input-file', str(table)] if task_type == 'batch_exec' else ['--interval', '3600']
    assert cli.main([*args, '--notify', 'succeeded,failed'], socket_path='test') == 2
    assert not seen
    assert cli.main([*args, '--notify', 'succeeded,failed', '--notify-email', 'test@example.com'], socket_path='test') == 0
    if task_type == 'batch_exec':
        body, _ = cli_tasks.prepare_batch(seen[0], {'workflow': {'start': {'node_type': 'StartNode', 'input_fields': {'value': {}}}}, 'version': 'v1.sv0'})
    else:
        body = cli_tasks._schedule_body(seen[0], create=True)
    assert body.notification_policy == {'enabled': True, 'on': ['succeeded', 'failed'], 'email': 'test@example.com'}
