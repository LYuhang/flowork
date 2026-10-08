import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { useExecutionHistory } from '../workflow-history';
import { sessionFetch } from '@/lib/api/session-fetch';
vi.mock('@/lib/api/session-fetch', () => ({ sessionFetch: vi.fn() }));
afterEach(cleanup);
for (const count of [1, 10, 50]) {
  it(`measures refreshing ${count} loaded pages using the production hook`, async () => {
    const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
    const rows = Array.from({ length: count * 25 }, (_, i) => ({ id: String(i), created_at: new Date(1800000000000-i*1000).toISOString(), status: 'succeeded', input_index: i }));
    const pages = Array.from({ length: count }, (_, i) => ({ items: rows.slice(i*25,(i+1)*25), has_more: i<count-1 }));
    const key = ['workflow-execution-history','task','measurement','all'];
    client.setQueryData(key, { pages, boundary: { before_id: rows.at(-1)!.id, before_time: rows.at(-1)!.created_at } });
    const request=vi.mocked(sessionFetch);request.mockReset();
    request.mockImplementation(async url => {
      const q=new URL(String(url),'http://localhost').searchParams;
      const offset=q.has('before_id')?Number(q.get('before_id'))+1:0;
      return new Response(JSON.stringify({items:rows.slice(offset,offset+Number(q.get('limit'))),has_more:offset+Number(q.get('limit'))<rows.length}));
    });
    const wrapper=({children}:{children:ReactNode})=><QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const hook=renderHook(()=>useExecutionHistory('task','measurement','all'),{wrapper});
    await act(async()=>{await hook.result.current.refetch();});
    await waitFor(()=>expect(hook.result.current.isFetching).toBe(false));
    expect(request.mock.calls.length).toBe(Math.ceil(count * 25 / 1000));
    expect(hook.result.current.data?.pages.flatMap(p=>p.items)).toEqual(rows);
    if (count === 50) {
      const added = Array.from({ length: 1050 }, (_, i) => ({ ...rows[0], id: `new-${i}`, created_at: new Date(1800001000000-i*1000).toISOString() }));
      const changed = [...added, ...rows.map((row, i) => i === rows.length - 1 ? { ...row, status: 'failed' } : row)];
      request.mockClear();
      request.mockImplementation(async url => {
        const params = new URL(String(url), 'http://localhost').searchParams;
        const offset = params.has('before_id') ? changed.findIndex(row => row.id === params.get('before_id')) + 1 : 0;
        return new Response(JSON.stringify({ items: changed.slice(offset, offset + 1000), has_more: offset + 1000 < changed.length }));
      });
      await act(async () => { await hook.result.current.refetch(); });
      await waitFor(() => expect(hook.result.current.data?.pages.flatMap(page => page.items)).toEqual(changed));
      expect(request).toHaveBeenCalledTimes(3);
      const retained = hook.result.current.data;
      request.mockImplementation(async url => {
        const params = new URL(String(url), 'http://localhost').searchParams;
        if (params.has('before_id')) throw new Error('read interrupted');
        return new Response(JSON.stringify({ items: changed.slice(0,1000), has_more: true }));
      });
      await act(async () => { await hook.result.current.refetch(); });
      await waitFor(() => expect(hook.result.current.isError).toBe(true));
      expect(hook.result.current.data).toEqual(retained);
    }
    hook.unmount();client.clear();
  });
}


it('retains the loaded boundary when an old approval leaves the filter, then pages below it', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  const key = ['workflow-execution-history','task','approval-changes','mine'];
  const row = (id: string, time: string) => ({id,created_at:time,status:'waiting_approval',input_index:0});
  const old = row('a', '2026-10-01T12:00:00.000500+00:00');
  const newer = row('b', '2026-10-01T12:00:00.000600+00:00');
  const older = row('z', '2026-10-01T12:00:00.000400+00:00');
  client.setQueryData(key, {pages:[{items:[old],has_more:true}],boundary:{before_id:old.id,before_time:old.created_at}});
  const request=vi.mocked(sessionFetch);request.mockReset();
  request.mockImplementation(async url => {
    const q=new URL(String(url),'http://localhost').searchParams;
    expect(q.get('mine')).toBe('true');
    if(q.has('before_id')) {
      expect(q.get('before_id')).toBe(old.id);
      return new Response(JSON.stringify({items:[older],has_more:false}));
    }
    return new Response(JSON.stringify({items:[newer,older],has_more:false}));
  });
  const wrapper=({children}:{children:ReactNode})=><QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const hook=renderHook(()=>useExecutionHistory('task','approval-changes','mine'),{wrapper});
  await act(async()=>{await hook.result.current.refetch();});
  await waitFor(() => expect(hook.result.current.data?.pages.flatMap(p=>p.items)).toEqual([newer]));
  expect(hook.result.current.hasNextPage).toBe(true);
  await act(async()=>{await hook.result.current.fetchNextPage();});
  await waitFor(() => expect(hook.result.current.data?.pages.flatMap(p=>p.items)).toEqual([newer,older]));
  expect(hook.result.current.hasNextPage).toBe(false);
  hook.unmount();client.clear();
});

it('keeps a late earlier-page response in its original filter cache', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const original = { id: 'head', created_at: '2026-10-01T12:00:00Z', status: 'succeeded', input_index: 0 };
  const older = { ...original, id: 'older', created_at: '2026-10-01T11:00:00Z' };
  const failed = { ...original, id: 'failure', status: 'failed' };
  const key = ['workflow-execution-history', 'task', 'switch-filter', 'all'];
  client.setQueryData(key, { pages: [{ items: [original], has_more: true }], boundary: { before_id: original.id, before_time: original.created_at } });
  let completePage!: (value: Response) => void;
  const request = vi.mocked(sessionFetch);
  request.mockReset();
  request.mockImplementation(async url => {
    const params = new URL(String(url), 'http://localhost').searchParams;
    if (params.has('before_id')) return new Promise(resolve => { completePage = resolve; });
    expect(params.get('statuses')).toBe('failed');
    return new Response(JSON.stringify({ items: [failed], has_more: false }));
  });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const hook = renderHook(({ filter }: { filter: 'all' | 'failed' }) => useExecutionHistory('task', 'switch-filter', filter), { wrapper, initialProps: { filter: 'all' } });
  let pending!: Promise<unknown>;
  act(() => { pending = hook.result.current.fetchNextPage(); });
  await waitFor(() => expect(completePage).toBeDefined());
  hook.rerender({ filter: 'failed' });
  await waitFor(() => expect(hook.result.current.data?.pages.flatMap(page => page.items)).toEqual([failed]));
  await act(async () => {
    completePage(new Response(JSON.stringify({ items: [older], has_more: false })));
    await pending;
  });
  expect(hook.result.current.data?.pages.flatMap(page => page.items)).toEqual([failed]);
  expect(client.getQueryData<{ pages: Array<{ items: unknown[] }> }>(key)?.pages.flatMap(page => page.items)).toEqual([original, older]);
  hook.unmount();
  client.clear();
});

it('allows loading earlier records during a background refresh and cancels that read', async () => {
  const client = new QueryClient();
  const head = { id: 'head', created_at: '2026-10-01T12:00:00Z', status: 'succeeded', input_index: 0 };
  const older = { ...head, id: 'older', created_at: '2026-10-01T11:00:00Z' };
  const key = ['workflow-execution-history', 'task', 'refresh-overlap', 'all'];
  client.setQueryData(key, { pages: [{ items: [head], has_more: true }], boundary: { before_id: head.id, before_time: head.created_at } });
  let started = false;
  let aborted = false;
  vi.mocked(sessionFetch).mockImplementation(async (url, init) => {
    const params = new URL(String(url), 'http://localhost').searchParams;
    if (params.has('before_id')) return new Response(JSON.stringify({ items: [older], has_more: false }));
    started = true;
    return new Promise((_, reject) => init?.signal?.addEventListener('abort', () => {
      aborted = true;
      reject(new DOMException('Aborted', 'AbortError'));
    }, { once: true }));
  });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const hook = renderHook(() => useExecutionHistory('task', 'refresh-overlap', 'all'), { wrapper });
  let refresh!: Promise<unknown>;
  act(() => { refresh = hook.result.current.refetch(); });
  await waitFor(() => expect(started).toBe(true));
  expect(hook.result.current.isFetchingNextPage).toBe(false);
  await act(async () => { await hook.result.current.fetchNextPage(); await refresh; });
  await waitFor(() => expect(hook.result.current.data?.pages.flatMap(page => page.items)).toEqual([head, older]));
  expect(aborted).toBe(true);
  hook.unmount();
  client.clear();
});
