"""Native fork contract, with a local provider and no credentials or model cost."""
from __future__ import annotations

import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from vibecanvas_api.services.agent_runtime.codex_app_server import CodexAppServer
from vibecanvas_api.services.codex_cli import resolve_codex_executable


@pytest.fixture
def local_provider():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            item = {
                'id': 'msg_fixture', 'type': 'message', 'role': 'assistant',
                'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'Fork fixture answer', 'annotations': []}],
            }
            response = {
                'id': 'resp_fixture', 'object': 'response', 'created_at': 1,
                'status': 'completed', 'model': 'gpt-5.6-terra', 'output': [item],
                'usage': {'input_tokens': 10, 'output_tokens': 4, 'total_tokens': 14},
            }
            events = [
                {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                {'type': 'response.output_item.added', 'output_index': 0,
                 'item': {**item, 'status': 'in_progress', 'content': []}},
                {'type': 'response.content_part.added', 'output_index': 0, 'item_id': item['id'],
                 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}},
                {'type': 'response.output_text.delta', 'output_index': 0, 'item_id': item['id'],
                 'content_index': 0, 'delta': 'Fork fixture answer'},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                {'type': 'response.completed', 'response': response},
            ]
            body = ''.join('data: ' + json.dumps(value) + '\n\n' for value in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
async def test_metadata_only_fork_preserves_native_completed_history(tmp_path, local_provider):
    executable = resolve_codex_executable()
    if executable is None:
        pytest.skip('Codex CLI is not installed')
    provider_url, calls = local_provider
    client = CodexAppServer(
        executable=executable,
        env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'CODEX_HOME': str(tmp_path)},
        cwd=str(tmp_path), experimental=True,
    )
    common = {
        'cwd': str(tmp_path), 'approvalPolicy': 'never', 'sandbox': 'read-only',
        'model': 'gpt-5.6-terra', 'modelProvider': 'fixture',
        'config': {
            'model_provider': 'fixture',
            'model_providers': {'fixture': {
                'name': 'Local protocol fixture', 'base_url': provider_url,
                'wire_api': 'responses', 'requires_openai_auth': False,
                'request_max_retries': 0, 'stream_max_retries': 0,
            }},
        },
    }
    try:
        await client.start()
        first = await client.request('thread/start', common, timeout_s=20)
        source_id = first['thread']['id']
        await client.request('turn/start', {
            'threadId': source_id,
            'input': [{'type': 'text', 'text': 'Metadata-only fork contract fixture'}],
        }, timeout_s=20)
        async with asyncio.timeout(40):
            async for event in client.messages():
                if event.get('method') == 'turn/completed':
                    assert event['params']['turn']['status'] == 'completed'
                    break
            else:
                pytest.fail('App-server closed before completing the fixture turn')
        fork = await client.request('thread/fork', {
            'threadId': source_id, **common, 'excludeTurns': True,
        }, timeout_s=20)
        assert fork['thread']['id'] != source_id
        assert fork['thread']['turns'] == []
        # Empty response history must not mean the underlying history was dropped.
        for thread_id in (source_id, fork['thread']['id']):
            stored = await client.request('thread/read', {
                'threadId': thread_id, 'includeTurns': True,
            }, timeout_s=20)
            contents = json.dumps(stored['thread']['turns'])
            assert 'Metadata-only fork contract fixture' in contents
            assert 'Fork fixture answer' in contents
        assert len(calls) == 1  # Forking never starts another model request.
    finally:
        await client.close()
