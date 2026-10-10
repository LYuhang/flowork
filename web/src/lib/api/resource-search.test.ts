import { afterEach, describe, expect, it, vi } from 'vitest';
import { searchResourcePage } from './resource-search';
import { listTasks, type TaskListResponse } from './tasks';
import { listDeployments, type DeploymentListResponse } from './deployments';

vi.mock('./tasks', () => ({ listTasks: vi.fn() }));
vi.mock('./deployments', () => ({ listDeployments: vi.fn() }));
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('resource name search transport', () => {
  it('sends encrypted title queries in the body and preserves the scan cursor and cancellation', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [], scanned: 100, next_cursor: 'next' })));
    vi.stubGlobal('fetch', fetch);
    const controller = new AbortController();
    const result = await searchResourcePage('chats', 'private [title]', 'previous', controller.signal);
    expect(fetch.mock.calls[0][0]).toMatch(/\/api\/v1\/search\/chats$/);
    expect(fetch.mock.calls[0][1].signal).toBe(controller.signal);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ query: 'private [title]', after_id: 'previous' });
    expect(result.next_cursor).toBe('next');
  });

  it('matches Task names rather than identifiers and advances past an empty filtered page', async () => {
    vi.mocked(listTasks).mockResolvedValueOnce({ items: [{ id: 'needle-id', payload: { name: 'Different name' }, task_type: 'batch_exec' }], total: 102, offset: 0, limit: 100 } as TaskListResponse)
      .mockResolvedValueOnce({ items: [{ id: 'match', payload: { name: 'NEEDLE task' }, task_type: 'batch_exec' }], total: 102, offset: 100, limit: 100 } as TaskListResponse);
    const signal = new AbortController().signal;
    const first = await searchResourcePage('tasks', 'needle', null, signal);
    expect(first.items).toEqual([]);
    expect(first.next_cursor).toBe('100');
    expect(listTasks).toHaveBeenCalledWith({ offset: 0, limit: 100 }, signal);
    const second = await searchResourcePage('tasks', 'needle', first.next_cursor, signal);
    expect(second.items).toEqual([{ id: 'match', title: 'NEEDLE task' }]);
    expect(second.next_cursor).toBeNull();
  });

  it('treats deployment wildcard characters as literal name text', async () => {
    vi.mocked(listDeployments).mockResolvedValue({ items: [{ id: 'one', name: 'A%_ demo' }, { id: 'two', name: 'Another demo' }], total: 2, offset: 0, limit: 100 } as DeploymentListResponse);
    const result = await searchResourcePage('deployments', '%_', null, new AbortController().signal);
    expect(result.items).toEqual([{ id: 'one', title: 'A%_ demo' }]);
  });

  it('propagates errors without an automatic retry', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('', { status: 503 }));
    vi.stubGlobal('fetch', fetch);
    await expect(searchResourcePage('projects', 'term', null, new AbortController().signal)).rejects.toThrow('503');
    expect(fetch).toHaveBeenCalledOnce();
  });
});
