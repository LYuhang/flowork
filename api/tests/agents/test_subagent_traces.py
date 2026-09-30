from copy import deepcopy
from types import SimpleNamespace

from vibecanvas_api.agents.tools.subagent.traces import chatml_messages


def test_chatml_roles_calls_and_tool_result_preserve_order():
    messages = [
        SimpleNamespace(type='human', content='Inspect image'),
        SimpleNamespace(type='ai', content='', tool_calls=[{'id': 'call1', 'name': 'read_images', 'args': {'paths': ['/data/image.png']}}]),
        SimpleNamespace(type='tool', name='read_images', tool_call_id='call1', content=[
            {'type': 'image', 'base64': 'SECRET_PIXELS', 'mime_type': 'image/png'},
            {'type': 'text', 'text': '/data/image.png'},
        ]),
    ]
    original = deepcopy(messages)
    trace = chatml_messages(messages)
    assert [m['role'] for m in trace] == ['user', 'assistant', 'tool']
    assert trace[1]['tool_calls'][0]['function']['name'] == 'read_images'
    assert trace[2]['tool_call_id'] == 'call1'
    assert trace[2]['content'][0]['base64'] == '[base64 omitted]'
    assert trace[2]['content'][0]['mime_type'] == 'image/png'
    assert messages == original
    assert 'SECRET_PIXELS' not in str(trace)


def test_nested_json_and_data_urls_omit_image_bytes():
    trace = chatml_messages([{'role': 'tool', 'content': '{"base64":"SECRET_PIXELS","url":"data:image/png;base64,AAAA"}'}])
    assert 'SECRET_PIXELS' not in str(trace)
    assert 'AAAA' not in str(trace)
    assert 'omitted' in str(trace)


def test_failed_node_trace_survives_event_mapping():
    import json
    from vibecanvas_api.services.exec_events import to_exec_update
    messages = [{"role": "assistant", "content": "partial result"}]
    _, frame = to_exec_update({"status": "error", "node_id": "worker",
                               "error_message": "provider unavailable",
                               "output": {"__traces__": messages}}, "run1")
    assert frame["status"] == "error"
    assert json.loads(frame["result"])["__traces__"] == messages


async def test_dispatch_retains_failure_traces_when_output_is_none():
    from vibecanvas_engine.nodes.exec import dispatch_node_call

    class FailedSubAgent:
        REQUIRES_THREAD_BRIDGE = True
        node_type = 'SubAgentNode'

        def __call__(self, inputs, previous_outputs, extra):
            extra['_subagent_traces'] = [{'role': 'tool', 'content': 'command failed'}]
            return {'status': 'error', 'output': None, 'error_message': 'failed'}

    shared = {}
    result = await dispatch_node_call(FailedSubAgent(), {}, {}, extra=shared)
    assert result['status'] == 'error'
    assert result['output']['__traces__'][0]['content'] == 'command failed'
    assert shared == {}


def test_batch_row_keeps_success_and_failure_node_traces():
    from vibecanvas_api.services.batch_runtime import _row_from_result
    messages = [{'role': 'tool', 'content': 'script completed'}]
    output = {'__traces__': messages, 'answer': 'verified'}
    for failed in (False, True):
        result = {'final_outputs': {} if failed else {'worker': output},
                  'error_dict': {'worker': {'status': 'error', 'output': output}} if failed else {},
                  'execution_time': 1.0}
        row = _row_from_result(index=0, original_input={}, mapped_input={},
                               previous=None, result_json=result, status_result={})
        actual = row['error']['details']['worker']['output'] if failed else row['output']['worker']
        assert actual['__traces__'] == messages
