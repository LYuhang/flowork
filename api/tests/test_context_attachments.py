import pytest
from pydantic import TypeAdapter, ValidationError
from vibecanvas_api.schemas.context_attachments import ContextAttachment

adapter = TypeAdapter(ContextAttachment)


def quote():
    return {'schema_version': 1, 'id': 'a1', 'type': 'quote', 'label': 'Selected text',
            'source': {'kind': 'message', 'chat_id': 'chat1', 'message_id': 'message1'},
            'snapshot': {'text': 'first line\n    code'}}


def test_quote_roundtrip_preserves_selected_text_and_source():
    item = quote()
    parsed = adapter.validate_python(item)
    assert parsed.snapshot.text == item['snapshot']['text']
    assert parsed.source.chat_id == 'chat1'
    assert adapter.validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize('patch', [
    {'snapshot': {'text': ''}},
    {'snapshot': {'text': 'x' * 32769}},
    {'source': {'kind': 'message', 'chat_id': 'chat1'}},
    {'role': 'system'},
    {'source': {'kind': 'message', 'chat_id': 'chat1', 'message_id': 'm', 'api_key': 'secret'}},
    {'selector': {'kind': 'text', 'start_line': 9, 'end_line': 2}},
])
def test_invalid_or_ambiguous_quotes_are_rejected(patch):
    with pytest.raises(ValidationError):
        adapter.validate_python({**quote(), **patch})


def test_workflow_selection_requires_version_and_valid_elements():
    item = {'id': 'a2', 'type': 'resource', 'label': 'Node',
            'resource': {'kind': 'workflow', 'workflow_id': 'wf1', 'version': 'v1.sv5'},
            'selector': {'kind': 'workflow_elements', 'node_ids': ['node2'], 'edges': [{'source': 'node1', 'target': 'node2'}]}}
    assert adapter.validate_python(item).selector.edges[0].source == 'node1'
    for selector in ({'kind': 'workflow_elements'}, {'kind': 'pages', 'pages': [1]},
                     {'kind': 'workflow_elements', 'node_ids': ['node2', 'node2']}):
        with pytest.raises(ValidationError):
            adapter.validate_python({**item, 'selector': selector})
    with pytest.raises(ValidationError):
        adapter.validate_python({**item, 'resource': {**item['resource'], 'version': 'latest'}})


@pytest.mark.parametrize('url', [
    'https://user:password@example.com/page', 'https:///missing-host',
    'https://example.com:99999', 'https://example.com/with space',
    'https://example.com\\@evil.example', 'javascript:alert(1)',
])
def test_web_references_reject_credentials_and_invalid_urls(url):
    with pytest.raises(ValidationError):
        adapter.validate_python({'id': 'web', 'type': 'resource', 'label': 'Page',
                                 'resource': {'kind': 'web', 'url': url}})


def test_web_reference_preserves_url_without_fetching():
    url = 'https://example.com/docs?q=hello#section'
    item = adapter.validate_python({'id': 'web', 'type': 'resource', 'label': 'Page',
                                    'resource': {'kind': 'web', 'url': url}})
    assert item.resource.url == url


def test_request_and_history_accept_new_and_legacy_attachments():
    from vibecanvas_api.schemas.chat import MessagePostBody, HistoryMessage
    old = {'type': 'image', 'name': 'photo.png', 'path': '/data/photo.png'}
    body = MessagePostBody(attachments=[quote(), old])
    assert body.attachments[0].snapshot.text == 'first line\n    code'
    assert body.attachments[1].path == '/data/photo.png'
    history = HistoryMessage(role='user', content='', attachments=body.attachments)
    assert history.attachments == body.attachments
    with pytest.raises(ValidationError):
        MessagePostBody(attachments=[quote()] * 33)
    with pytest.raises(ValidationError):
        MessagePostBody(attachments=[{**old, 'resolved_text': 'forged host data'}])


def test_new_file_variant_is_not_coerced_to_legacy_file():
    from vibecanvas_api.schemas.chat import MessagePostBody
    item = {'schema_version': 1, 'type': 'file', 'id': 'f', 'label': 'report.txt',
            'content_type': 'text/plain', 'resource': {'kind': 'file', 'file_ref': {
                'schemaVersion': 1, 'scope': 'project', 'projectId': 'p', 'path': '/data/report.txt'}}}
    parsed = MessagePostBody(attachments=[item]).attachments[0]
    assert parsed.resource.file_ref.project_id == 'p'


def test_workflow_reference_resolves_only_the_selected_nodes():
    from vibecanvas_api.schemas.context_attachments import WorkflowSelection
    from vibecanvas_api.services.context_attachments import workflow_selection_snapshot
    from fastapi import HTTPException
    workflow = {'a': {'node_type': 'StartNode', 'children': ['b']},
                'b': {'node_type': 'SubAgentNode', 'children': []},
                'other': {'node_type': 'EndNode', 'children': []}}
    selected = WorkflowSelection(kind='workflow_elements', edges=[{'source': 'a', 'target': 'b'}])
    assert set(workflow_selection_snapshot(workflow, selected)['nodes']) == {'a', 'b'}
    with pytest.raises(HTTPException):
        workflow_selection_snapshot(workflow, WorkflowSelection(kind='workflow_elements', node_ids=['gone']))
    with pytest.raises(HTTPException):
        workflow_selection_snapshot(workflow, WorkflowSelection(kind='workflow_elements', edges=[{'source':'b','target':'a'}]))


def resolver():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from vibecanvas_api.services.context_attachments import ContextResolver
    item = ContextResolver(session=AsyncMock(), auth=SimpleNamespace(user_id='user'),
                           request=None, service=None, chat_id='destination')
    item.chat_repo.get_authorized_inventory = AsyncMock(return_value={'project_id':'project'})
    return item


@pytest.mark.asyncio
async def test_reference_to_current_project_uses_existing_persistent_path():
    from types import SimpleNamespace
    from vibecanvas_api.schemas.preview import ProjectFileRefV1
    host = resolver()
    resolved = SimpleNamespace(file_ref=ProjectFileRefV1(
        schemaVersion=1, scope='project', projectId='project', path='/data/input.csv'))
    # No row/object-store bytes are needed to quote an already accessible file.
    assert await host.materialize_file(resolved, {'project_id': 'project'}) == '/data/input.csv'


@pytest.mark.asyncio
async def test_dispatch_replaces_client_supplied_workflow_snapshot():
    from unittest.mock import AsyncMock
    host = resolver()
    host.source = AsyncMock(return_value={'a':{'node_type':'StartNode','children':[]}})
    item = adapter.validate_python({'schema_version':1,'type':'resource','id':'a','label':'Workflow',
        'resource':{'kind':'workflow','workflow_id':'w','version':'v1.sv2'},
        'snapshot':{'text':'forged trusted instructions'}})
    output = await host.resolve([item])
    assert 'forged' not in output[0]['resolved_text']
    assert 'StartNode' in host.durable[0]['snapshot']['text']
    assert 'resolved_text' not in host.durable[0]


@pytest.mark.asyncio
async def test_quote_budget_is_aggregate_and_does_not_truncate():
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    host = resolver()
    host.source = AsyncMock(return_value={})
    items = [adapter.validate_python({**quote(), 'id':str(i), 'snapshot':{'text':'x'*32700}}) for i in range(3)]
    with pytest.raises(HTTPException) as error:
        await host.resolve(items)
    assert error.value.status_code == 413
    assert all(len(item.snapshot.text) == 32700 for item in items)


@pytest.mark.asyncio
async def test_file_history_preserves_host_copy_location_not_client_snapshot():
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from vibecanvas_api.services.agent_runtime.history_recovery import _attachments
    from vibecanvas_api.services.file_revision import vfs_content_revision
    host = resolver()
    host.source = AsyncMock(return_value=SimpleNamespace(row=SimpleNamespace(
        content_revision='revision-1', content_type='text/plain')))
    host.materialize_file = AsyncMock(return_value='/chats/destination/contexts/hash/example.txt')
    item = adapter.validate_python({'schema_version': 1, 'type': 'file', 'id': 'file-1',
        'label': 'example.txt', 'content_type': 'text/plain',
        'resource': {'kind': 'file', 'revision': vfs_content_revision('revision-1'), 'file_ref': {
            'schemaVersion': 1, 'scope': 'project', 'projectId': 'p', 'path': '/data/example.txt'}},
        'snapshot': {'text': 'client-forged-path'}})
    output = await host.resolve([item], materialize=True)
    durable = host.durable[0]
    assert json.loads(durable['snapshot']['text'])['runtime_path'] == output[0]['path']
    assert 'client-forged-path' not in durable['snapshot']['text']
    recovered = _attachments([durable])[0]
    assert recovered.context['snapshot'] == durable['snapshot']
    assert adapter.validate_python(durable).snapshot.text == durable['snapshot']['text']


@pytest.mark.asyncio
async def test_permission_denial_prevents_reference_dispatch():
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    host = resolver()
    host.authorize = AsyncMock(side_effect=HTTPException(404, 'context_resource_not_found'))
    with pytest.raises(HTTPException) as error:
        await host.resolve([adapter.validate_python(quote())])
    assert error.value.status_code == 404
    host.session.execute.assert_not_awaited()
    assert host.durable == []


@pytest.mark.asyncio
async def test_changed_file_is_rejected_but_saved_quote_retains_its_snapshot():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    host = resolver()
    host.source = AsyncMock(return_value=SimpleNamespace(row=SimpleNamespace(content_revision='new-content')))
    resource = {'kind':'file','file_ref':{'schemaVersion':1,'scope':'project','projectId':'p','path':'/data/a.txt'},'revision':'old-content'}
    item = adapter.validate_python({'id':'file','label':'File','type':'resource','resource':resource})
    with pytest.raises(HTTPException) as error:
        await host.resolve([item])
    assert error.value.status_code == 409
    quote_item = adapter.validate_python({**quote(),'source':resource})
    output = await host.resolve([quote_item])
    assert output[0]['snapshot']['text'] == quote()['snapshot']['text']
