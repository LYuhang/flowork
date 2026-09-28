#!/usr/bin/env python3
"""Submit the UI's automation example once, then independently verify its API.

Uses the API environment's requests dependency. Set FLOWORK_TEST_EMAIL and
FLOWORK_TEST_PASSWORD; evidence (including the private deployment credential)
is stored in a NEW --evidence-dir. Never sends follow-up prompts or edits graphs.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--language', choices=['zh', 'en'], default='zh')
    parser.add_argument('--model', default='codex:account:gpt-6-sol')
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    if urlsplit(base).scheme not in {'https', 'http'}:
        parser.error('base-url must use HTTP(S)')
    email = os.environ.get('FLOWORK_TEST_EMAIL')
    password = os.environ.get('FLOWORK_TEST_PASSWORD')
    if not email or not password:
        parser.error('Set FLOWORK_TEST_EMAIL and FLOWORK_TEST_PASSWORD')
    os.umask(0o077)
    root = args.evidence_dir
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    state = {'model': args.model, 'reasoning_effort': 'high', 'message_count': 0,
             'started_at': time.time(), 'status': 'initializing'}
    def save_state():
        (root / 'state.json').write_text(json.dumps(state, ensure_ascii=False, indent=2))
    session = requests.Session()
    session.headers['Origin'] = base
    def call(method, path, **kwargs):
        response = session.request(method, base + path, timeout=(20, 600), allow_redirects=False, **kwargs)
        response.raise_for_status()
        if response.is_redirect:
            raise RuntimeError('Unexpected API redirect')
        return response
    try:
        call('POST', '/api/v1/auth/login', json={'email': email, 'password': password})
        csrf = next((cookie.value for cookie in session.cookies if cookie.name.endswith('vibecanvas-web-csrf')), None)
        if not csrf:
            raise RuntimeError('Login did not provide a CSRF cookie')
        session.headers['X-CSRF-Token'] = csrf
        project = call('POST', '/api/v1/projects', json={'name': 'Automation acceptance · Order audit API'}).json()['project_id']
        carrier = call('GET', '/api/v1/chats/bootstrap').json()['carrier_scope_id']
        chat = 'automation_order_' + uuid.uuid4().hex[:12]
        path = f'/api/v1/chat-scopes/{carrier}/chats/{chat}'
        call('PUT', path, json={'project_id': project})
        locale_path = Path(__file__).resolve().parents[1] / f'web/src/lib/i18n/locales/{args.language}.json'
        prompt = json.loads(locale_path.read_text())['chat.examples.automation.orderAudit.prompt'].replace('{{publicUrl}}', base)
        (root / 'prompt.txt').write_text(prompt)
        state.update(project_id=project, chat_id=chat, carrier_scope_id=carrier,
                     message_count=1, status='running')
        save_state()
        print(json.dumps({'project_id': project, 'chat_id': chat, 'message_count': 1}), flush=True)
        # Exactly one user message. There is deliberately no retry/repair path.
        response = session.post(base + path + '/messages', json={
            'role': 'user', 'content': prompt, 'project_id': project,
            'client_request_id': uuid.uuid4().hex, 'mode': 'chat', 'surface': 'main',
            'agent_surface': 'chat', 'approval_mode': 'always_allow',
            'agent_settings': {'model_id': args.model, 'reasoning_effort': 'high'},
        }, stream=True, timeout=(20, 3600), allow_redirects=False)
        response.raise_for_status()
        state['turn_id'] = response.headers.get('X-Turn-Id')
        save_state()
        event = ''
        count = 0
        with response, (root / 'events.jsonl').open('w') as log:
            for line in response.iter_lines(decode_unicode=True):
                if line and line.startswith('event:'):
                    event = line[6:].strip()
                elif line and line.startswith('data:'):
                    data = json.loads(line[5:])
                    log.write(json.dumps({'at': time.time(), 'event': event, 'data': data}, ensure_ascii=False) + '\n')
                    log.flush()
                    count += 1
                    if count % 100 == 0:
                        print(f'events={count}', flush=True)
                    if event in {'done', 'error', 'failed'}:
                        state['status'] = event
        state['events'] = count
        state['finished_at'] = time.time()
        save_state()
        if state['status'] != 'done':
            raise RuntimeError('Agent did not complete successfully; inspect private events.jsonl')
        storage = f'/project/{project}/chats/{chat}/'
        for name in ('manifest.json', 'README.md'):
            data = call('GET', '/api/v1/storage/raw', params={'path': storage + 'automation-order-audit/' + name}).content
            (root / name).write_bytes(data)
        manifest = json.loads((root / 'manifest.json').read_text())
        credential = manifest['credential_file']
        if credential.startswith(f'/chats/{chat}/'):
            credential = credential.removeprefix(f'/chats/{chat}/')
        if credential != '.secrets/order-audit.json':
            raise RuntimeError('Manifest does not point to the requested private credential file')
        (root / 'credential.json').write_bytes(call('GET', '/api/v1/storage/raw', params={'path': storage + credential}).content)
        graph = call('GET', f"/api/v1/workflows/{manifest['workflow_id']}/at/{manifest['version']}").json()
        (root / 'saved-graph.json').write_text(json.dumps(graph, ensure_ascii=False))
        endpoint = manifest['endpoint']
        if urlsplit(endpoint).netloc != urlsplit(base).netloc or urlsplit(endpoint).scheme != urlsplit(base).scheme:
            raise RuntimeError('Refusing to send deployment credential to a different origin')
        subprocess.run([sys.executable, str(Path(__file__).with_name('verify_order_audit_api.py')),
                        '--endpoint', endpoint, '--credential-file', str(root / 'credential.json'),
                        '--workflow-json', str(root / 'saved-graph.json'), '--report', str(root / 'external-report.json')], check=True)
        state['verified'] = True
        print(json.dumps({'verified': True, 'workflow_id': manifest['workflow_id'], 'endpoint': endpoint}), flush=True)
    finally:
        save_state()
        session.close()


if __name__ == '__main__':
    main()
