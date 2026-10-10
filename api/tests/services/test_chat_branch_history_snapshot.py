from copy import deepcopy

from vibecanvas_api.services.chat_branch_history import branch_message_snapshot


def test_branch_freezes_authoritative_interaction_and_preserves_file_references():
    attachment = {'type': 'file', 'name': 'report.pdf', 'path': '/data/source/report.pdf'}
    row = {'message_id': 'answer', 'turn_id': 'turn', 'role': 'assistant', 'meta': {'custom': 'value'},
           'content': {'text': 'Prior response', 'attachments': [attachment], 'tool_calls': [
               {'id': 'choice', 'name': 'ask', 'artifact': {'meta': {'pending_approval': True}}},
           ]}}
    resolved = {'payload': {'artifact': {'kind': 'interactive_artifact', 'hitl_request_id': 'original',
        'interaction_state': {'status': 'submitted', 'result': {'selected_ids': ['yes']}}},
        'pending_approval': True}, 'meta': {'pending_approval': True}}
    original = deepcopy((row, resolved))
    snapshot = branch_message_snapshot(row, source_chat_id='source', tool_projections={'choice': resolved})
    artifact = snapshot['content']['tool_calls'][0]['artifact']
    assert artifact['payload']['artifact']['read_only'] is True
    assert artifact['payload']['artifact']['interaction_state'] == resolved['payload']['artifact']['interaction_state']
    assert artifact['payload']['pending_approval'] is False
    assert artifact['meta']['pending_approval'] is False
    assert snapshot['content']['attachments'] == [attachment]
    assert snapshot['meta']['branch_source'] == {'chat_id': 'source', 'turn_id': 'turn', 'message_id': 'answer'}
    assert (row, resolved) == original


def test_branch_marks_direct_tool_artifacts_read_only_without_inventing_a_decision():
    row = {'message_id': 'tool-result', 'turn_id': 'turn', 'role': 'tool', 'meta': {},
           'content': {'text': 'Question', 'tool_call_id': 'choice', 'artifact': {
               'kind': 'interactive_artifact', 'interaction_state': {'status': 'pending'},
           }}}
    snapshot = branch_message_snapshot(row, source_chat_id='source', tool_projections={})
    assert snapshot['content']['artifact']['read_only'] is True
    assert snapshot['content']['artifact']['interaction_state']['status'] == 'pending'
    assert 'read_only' not in row['content']['artifact']
