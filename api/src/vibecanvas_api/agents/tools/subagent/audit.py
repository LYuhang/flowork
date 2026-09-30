"""Bounded resource-use evidence; never persist prompts or raw tool bodies."""
from __future__ import annotations
import json
import re

_SKILL_PATH = re.compile(r'/skills/([a-f0-9-]{36})/([a-f0-9]{64})/([A-Za-z0-9_./-]{1,240})')


class ResourceAudit:
    def __init__(self, snapshot):
        self.payload = {
            'skills': [{k: s[k] for k in ('id', 'name', 'revision_hash') if k in s}
                       for s in snapshot.get('skills', [])],
            'mcp_servers': [{k: s[k] for k in ('id', 'name', 'tools_fingerprint') if k in s}
                            for s in snapshot.get('mcp_servers', [])],
            'tools': [], 'truncated': False,
        }
        self.pending = {}

    async def record(self, entry):
        records = self.payload['tools']
        for call in entry.get('tool_calls', []):
            if len(records) >= 256:
                self.payload['truncated'] = True
                break
            item = {'name': str(call.get('name', ''))[:128], 'status': 'requested'}
            if item['name'] == 'bash':
                command = (call.get('args') or {}).get('command', '')
                item['skill_paths'] = sorted({m.group(0) for m in _SKILL_PATH.finditer(str(command))
                                             if '..' not in m.group(3).split('/')})[:32]
            records.append(item)
            if call.get('id'):
                self.pending[call['id']] = item
        if entry.get('role') != 'tool':
            return
        item = self.pending.pop(entry.get('tool_call_id'), None)
        if item is None:
            return
        item['status'] = 'error' if entry.get('status') == 'error' else 'returned'
        if item['name'] == 'bash':
            try:
                result = json.loads(entry.get('text') or '')
            except (ValueError, TypeError):
                return
            if isinstance(result, dict):
                if type(result.get('exit_code')) is int:
                    item['exit_code'] = result['exit_code']
                if result.get('status') in ('success', 'error'):
                    item['status'] = result['status']
