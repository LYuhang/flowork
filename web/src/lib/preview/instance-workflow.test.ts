import { beforeEach, expect, it, vi } from 'vitest';
import { loadInstanceWorkflow } from './instance-workflow';
import { sessionFetch } from '@/lib/api/session-fetch';
vi.mock('@/lib/api/session-fetch', () => ({ sessionFetch: vi.fn() }));
vi.mock('@/lib/base-path', () => ({ resolveApiUrl: (path: string) => path }));
beforeEach(() => vi.clearAllMocks());
it('reads the execution API wf_id contract and rejects a different version', async () => {
  vi.mocked(sessionFetch).mockResolvedValue(new Response(JSON.stringify({ wf_id: 'wf', workflow_version: 'v1.sv2', workflow: { node_1: {} } })));
  const result = await loadInstanceWorkflow({ type: 'execution', id: 'run' }, 'wf', 'v1.sv2');
  expect(result.workflow).toHaveProperty('node_1');
  vi.mocked(sessionFetch).mockResolvedValue(new Response(JSON.stringify({ wf_id: 'wf', workflow_version: 'v1.sv3', workflow: {} })));
  await expect(loadInstanceWorkflow({ type: 'execution', id: 'run' }, 'wf', 'v1.sv2')).rejects.toThrow('does not match');
});
it('does not return a cached snapshot when the instance is inaccessible', async () => {
  vi.mocked(sessionFetch).mockResolvedValue(new Response('{}', { status: 404 }));
  await expect(loadInstanceWorkflow({ type: 'task', id: 'task' }, 'wf', 'v1.sv2')).rejects.toThrow('unavailable');
});
