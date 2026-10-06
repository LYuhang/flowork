import asyncio
import io
import json
import pytest
from vibecanvas_api.flowork_cli.local_workflow import run_rows

@pytest.mark.asyncio
async def test_local_rows_reach_terminal_without_host(monkeypatch):
    from vibecanvas_engine.workflow import Workflow
    active=maximum=0
    async def stream(self, inputs, run_context=None, stop_event=None):
        nonlocal active,maximum
        active+=1;maximum=max(maximum,active)
        try:
            await asyncio.sleep(.01)
            yield {'status':'finished','final_outputs':{'__end__':inputs},'error_dict':{},'execution_time':.01}
        finally:active-=1
    monkeypatch.setattr(Workflow, 'check', lambda graph: {'status': 'success'})
    monkeypatch.setattr(Workflow,'_build_id2node',lambda self,graph:{})
    monkeypatch.setattr(Workflow,'astream',stream)
    out=io.StringIO(); events=io.StringIO();states=[]
    code=await run_rows(workflow={},rows=[{'x':i} for i in range(20)],concurrency=2,
        output=out,events=events,status=states.append,context_factory=lambda i:{})
    records=[json.loads(s) for s in out.getvalue().splitlines()]
    assert code==0 and len(records)==20 and maximum==2
    assert sorted(r['index'] for r in records)==list(range(20))
    assert all(r['output']==r['input'] for r in records)
    assert states[-1]['status']=='completed' and states[-1]['completed']==20

@pytest.mark.asyncio
async def test_missing_terminal_is_explicit_failure(monkeypatch):
    from vibecanvas_engine.workflow import Workflow
    async def stream(self, inputs, run_context=None, stop_event=None):
        yield {'status':'running'}
    monkeypatch.setattr(Workflow, 'check', lambda graph: {'status': 'success'})
    monkeypatch.setattr(Workflow,'_build_id2node',lambda self,graph:{})
    monkeypatch.setattr(Workflow,'astream',stream)
    states=[]
    output = io.StringIO()
    code = await run_rows(workflow={},rows=[{}],concurrency=1,output=output,
        events=io.StringIO(),status=states.append,context_factory=lambda i:{})
    assert code == 1 and states[-1]['completed'] == 1
    assert 'without a terminal' in json.loads(output.getvalue())['errors']['__engine__']



def test_cli_local_command_closes_authorization_connection_before_execution(tmp_path, monkeypatch):
    from vibecanvas_api.flowork_cli import cli, local_workflow, local_command
    from vibecanvas_api.services.sandbox.local_activity import execution_activity
    monkeypatch.setattr(local_command, 'execution_activity', lambda **kwargs: execution_activity(tmp_path / 'work', **kwargs))
    import asyncio
    input_path=tmp_path/'inputs.jsonl';input_path.write_text('{"x":1}\n')
    output_path=tmp_path/'results.jsonl'
    args=cli.parser().parse_args(['workflow','run-batch','--workflow-id','wf','--major','v1',
        '--input-file',str(input_path),'--output',str(output_path),'--concurrency','2'])
    calls=[]
    def request(endpoint, arguments, *, operation):
        calls.append(operation)
        return {'workflow':{},'context':{},'id':'wf','version':'v1.sv0','source':'saved'}
    monkeypatch.setattr(cli,'request',request)
    async def run_rows(**kwargs):
        assert calls==['workflow.prepare']
        kwargs['output'].write('{"index":0,"status":"success"}\n')
        kwargs['status']({'status':'completed','completed':1,'failed':0})
        return 0
    monkeypatch.setattr(local_workflow,'run_rows',run_rows)
    assert cli.execute_command(args,'unused')==0
    states=list((tmp_path/'.flowork-runs').glob('*/status.json'))
    summary=json.loads(states[0].read_text())
    assert summary['status']=='completed' and summary['completed']==1 and summary['exit_code']==0
    assert calls==['workflow.prepare']


@pytest.fixture
def executable_graph():
    from pathlib import Path
    import vibecanvas_engine
    path = Path(vibecanvas_engine.__file__).resolve().parents[2] / "tests" / "example_workflow.json"
    return json.loads(path.read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("selected_node", [None, "node_2"])
async def test_real_engine_batch_and_standalone(executable_graph, selected_node):
    graph = executable_graph
    if selected_node:
        graph = {key: value for key, value in graph.items() if key in {"__meta__", selected_node}}
    output, events, states = io.StringIO(), io.StringIO(), []
    code = await run_rows(workflow=graph, rows=[{"text": "hi", "count": n} for n in (1, 2, 3)],
        concurrency=2, output=output, events=events, status=states.append,
        context_factory=lambda index: {}, node_id=selected_node)
    records = sorted((json.loads(line) for line in output.getvalue().splitlines()), key=lambda row: row["index"])
    assert code == 0
    assert [row["output"]["repeated"] for row in records] == ["hi", "hi hi", "hi hi hi"]
    assert all(row["status"] == "success" for row in records)
    assert states[-1]["completed"] == 3
    assert states[-1]["status"] == "completed"


@pytest.mark.asyncio
async def test_real_engine_row_failure_does_not_drop_other_rows(executable_graph):
    executable_graph["node_2"]["node_config"]["process_fn"] = (
        "def process_fn(inputs):\n"
        "    if inputs['count'] == 2: raise ValueError('intentional sample failure')\n"
        "    return {'repeated': inputs['text'], 'char_count': len(inputs['text'])}"
    )
    output, states = io.StringIO(), []
    code = await run_rows(workflow=executable_graph, rows=[{"text": "hi", "count": n} for n in (1, 2, 3)],
        concurrency=2, output=output, events=io.StringIO(), status=states.append,
        context_factory=lambda index: {})
    records = sorted((json.loads(line) for line in output.getvalue().splitlines()), key=lambda row: row["index"])
    assert code == 1
    assert [row["status"] for row in records] == ["success", "error", "success"]
    assert records[1]["errors"]
    assert states[-1]["completed"] == 3 and states[-1]["failed"] == 1
    assert states[-1]["status"] == "completed_with_errors"


@pytest.mark.asyncio
async def test_cancellation_awaits_all_local_workers(monkeypatch):
    from vibecanvas_engine.workflow import Workflow
    started = asyncio.Event()
    active = 0
    async def stream(self, inputs, run_context=None, stop_event=None):
        nonlocal active
        active += 1
        if active == 2:
            started.set()
        try:
            await stop_event.wait()
            yield {}
        finally:
            active -= 1
    monkeypatch.setattr(Workflow, 'check', lambda graph: {'status': 'success'})
    monkeypatch.setattr(Workflow, '_build_id2node', lambda self, graph: {})
    monkeypatch.setattr(Workflow, 'astream', stream)
    states, output = [], io.StringIO()
    task = asyncio.create_task(run_rows(workflow={}, rows=[{}, {}, {}], concurrency=2,
        output=output, events=io.StringIO(), status=states.append, context_factory=lambda index: {}))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert active == 0
    assert output.getvalue() == ''
    assert states[-1]['status'] == 'cancelled' and states[-1]['completed'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,delay", [(True, 0), (False, 0), (None, 0), (True, 1.2)])
async def test_local_human_approval_preserves_decision_and_timeout(executable_graph, decision, delay):
    graph = executable_graph
    graph['__meta__']['settings'] = {'timeouts': {'workflow': 1}}
    human = graph['node_2']
    human['node_type'] = 'HumanApprovalNode'
    human['node_name'] = 'review'
    human['node_config'] = {'instruction': 'Review sample', 'timeout_seconds': 2}
    human['output_fields'] = {'approved': {'type': 'boolean', 'description': 'Decision'}}
    graph['node_3']['input_fields'] = {'approved': {'type': 'boolean', 'value': False, 'reference': 'review.approved'}}
    graph['node_3']['output_fields'] = human['output_fields']
    async def approve(runtime, invocation_id, index, event):
        if decision is not None:
            await asyncio.sleep(delay)
            await runtime.decide(invocation_id, event['approval_id'], decision)
    output, states = io.StringIO(), []
    code = await run_rows(workflow=graph, rows=[{'text': 'review', 'count': 1}], concurrency=1,
        output=output, events=io.StringIO(), status=states.append,
        context_factory=lambda index: {}, approval_handler=approve)
    record = json.loads(output.getvalue())
    if decision is None:
        assert code == 1
        assert record['status'] == 'timed_out'
        assert record['error_code'] == 'approval_timeout'
        assert record['output'] is None
    else:
        assert code == 0
        assert record['output'] == {'approved': decision}
    assert states[-1]['completed'] == 1


@pytest.mark.asyncio
async def test_local_runtime_enforces_whole_workflow_timeout(executable_graph):
    executable_graph['__meta__']['settings'] = {'timeouts': {'workflow': 1}}
    executable_graph['node_2']['node_config']['process_fn'] = (
        "import time\ndef process_fn(inputs):\n    time.sleep(10)\n"
        "    return {'repeated': 'late', 'char_count': 4}"
    )
    output = io.StringIO()
    code = await asyncio.wait_for(run_rows(workflow=executable_graph,
        rows=[{'text': 'hi', 'count': 1}], concurrency=1, output=output,
        events=io.StringIO(), status=lambda value: None, context_factory=lambda index: {}), 5)
    record = json.loads(output.getvalue())
    assert code == 1
    assert record['status'] == 'timed_out'
    assert record['error_code'] == 'execution_timeout'
    assert record['output'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('human', [False, True, 'cancel'])
async def test_local_approval_transport_only_contacts_platform_for_human(executable_graph, tmp_path, human):
    import httpx
    from vibecanvas_api.flowork_cli.local_approval_client import LocalApprovalClient
    graph = executable_graph
    if human:
        graph['node_2'].update(node_type='HumanApprovalNode', node_name='review',
            node_config={'instruction': 'Review sample', 'timeout_seconds': 5},
            output_fields={'approved': {'type': 'boolean', 'description': 'Decision'}})
        graph['node_3']['input_fields'] = {'approved': {'type': 'boolean', 'value': False, 'reference': 'review.approved'}}
        graph['node_3']['output_fields'] = graph['node_2']['output_fields']
    calls, frames, progress = [], [], []
    waiting = asyncio.Event()
    def report_progress(value):
        progress.append(value)
        waiting.set()
    def handle(request):
        assert request.headers['authorization'] == 'Bearer private-test-token'
        calls.append(request.url.path)
        body = json.loads(request.content)
        if request.url.path.endswith('/events'):
            frames.extend(body['events'])
            return httpx.Response(200, json={'last_seq': frames[-1]['seq']})
        pending = next(frame for frame in frames if frame['type'] == 'approval_requested')
        if human == 'cancel':
            return httpx.Response(200, json={'cancel_requested': False, 'decisions': []})
        return httpx.Response(200, json={'cancel_requested': False, 'decisions': [
            {'id': pending['approval_id'], 'requested_decision': True}]})
    rows = [{'text': 'hi', 'count': 1}]
    path = tmp_path/'events.jsonl'
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        bridge = LocalApprovalClient(descriptor={'url': 'https://platform.test/approval', 'capability': 'private-test-token'},
            workflow=graph, rows=rows, events_path=path, progress=report_progress, client=http)
        output = io.StringIO()
        with path.open('w') as events:
            task = asyncio.create_task(run_rows(workflow=graph, rows=rows, concurrency=1, output=output,
                events=events, status=lambda state: None, context_factory=lambda index: {},
                approval_handler=bridge.approve, event_sink=bridge.event))
            if human == 'cancel':
                await asyncio.wait_for(waiting.wait(), 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
            else:
                assert await task == 0
    if human == 'cancel':
        assert frames[-1]['type'] == 'result' and frames[-1]['status'] == 'cancelled'
    elif human:
        assert json.loads(output.getvalue())['output'] == {'approved': True}
        assert frames[-1]['type'] == 'result' and frames[-1]['status'] == 'succeeded'
        assert [frame['seq'] for frame in frames] == list(range(1, frames[-1]['seq'] + 1))
        assert progress[0]['execution_id'] == frames[0]['invocation_id']
        assert any(path.endswith('/decisions') for path in calls)
    else:
        assert calls == []
    assert 'private-test-token' not in path.read_text()
