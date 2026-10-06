"""Approval-only HTTP transport; ordinary inference never calls this service."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx


class LocalApprovalClient:
    def __init__(self, *, descriptor, workflow, rows, events_path, progress, client=None):
        self.descriptor, self.workflow, self.rows = descriptor, workflow, rows
        self.events_path, self.progress = Path(events_path), progress
        self.client = client or httpx.AsyncClient(timeout=30)
        self.owns_client = client is None
        self.registered = set()
        self.acknowledged = {}
        self.locks = {}

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def _post(self, operation, body):
        response = await self.client.post(self.descriptor['url'] + '/' + operation,
            headers={'Authorization': 'Bearer ' + self.descriptor['capability']}, json=body)
        if response.status_code != 200:
            # Never include server bodies, signed tokens or request headers.
            raise RuntimeError(f'Approval service failed (HTTP {response.status_code})')
        return response.json()

    async def event(self, runtime, invocation_id, index, event):
        if index not in self.registered and event['type'] != 'approval_requested':
            return
        if event['type'] not in {'approval_requested', 'approval_resolved', 'result'}:
            return
        async with self.locks.setdefault(index, asyncio.Lock()):
            # Read the existing local event file, not a second in-memory trace.
            frames = []
            with self.events_path.open(encoding='utf-8') as source:
                for line in source:
                    frame = json.loads(line)
                    if frame.pop('index') != index or not self.acknowledged.get(index, 0) < frame['seq'] <= event['seq']:
                        continue
                    frames.append(frame)
                    if len(frames) == 100:
                        await self._send(index, frames)
                        frames = []
            if frames:
                await self._send(index, frames)
        if event['type'] == 'approval_requested':
            self.progress({'row_status': 'waiting_approval', 'index': index,
                'execution_id': invocation_id, 'execution_url': f'/workflow-executions/{invocation_id}',
                'approval_id': event['approval_id'], 'deadline': event['deadline']})

    async def _send(self, index, frames):
        response = await self._post('events', {'index': index, 'workflow': self.workflow,
            'inputs': self.rows[index], 'events': frames})
        if response.get('last_seq') != frames[-1]['seq']:
            raise RuntimeError('Approval evidence acknowledgement mismatch')
        self.acknowledged[index] = response['last_seq']
        self.registered.add(index)

    async def approve(self, runtime, invocation_id, index, event):
        while runtime.status(invocation_id)['status'] not in runtime.TERMINAL:
            response = await self._post('decisions', {'index': index})
            if response['cancel_requested']:
                await runtime.cancel(invocation_id)
                return
            for decision in response['decisions']:
                if decision['id'] == event['approval_id']:
                    from vibecanvas_engine.runtime.approvals import ApprovalConflict
                    try:
                        await runtime.decide(invocation_id, event['approval_id'], decision['requested_decision'])
                    except ApprovalConflict:
                        item = runtime.executions[invocation_id].approvals.approvals[event['approval_id']]
                        if item.reason not in {'timeout', 'cancelled'}:
                            raise
                    return
            if event['approval_id'] not in {item['approval_id'] for item in runtime.status(invocation_id).get('approvals', [])}:
                return
            await asyncio.sleep(1)
