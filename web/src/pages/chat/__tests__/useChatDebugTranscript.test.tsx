import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readDebugIncrement, useChatDebugTranscript } from '../useChatDebugTranscript';
import { useChatStreamStore } from '@/stores/chat-stream';

const message = (cursor: number, role = 'user') => ({ cursor, id: `m${cursor}`, role, content: `Message ${cursor}`, turn_id: 't1', ts: 1, tool_calls: [], attachments: [], meta: {} });
const page = (chat_id: string, n: number, has_more = false) => ({ chat_id, messages: [message(n)], next_cursor: n, has_more, artifacts: [{ artifact_id: 'artifact', artifact: { props: { version: 'v1.sv3' } } }], turns: [{ run_id: 't1', status: 'completed' }] });
afterEach(() => { vi.unstubAllGlobals(); useChatStreamStore.getState().reset(); });

describe('Database debug transcript', () => {
  it('refreshes on durable acceptance and completion, not optimistic send or every token', async () => {
    let requests = 0;
    const fetch = vi.fn(async () => Response.json(page('a', ++requests)));
    vi.stubGlobal('fetch', fetch);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result, unmount } = renderHook(() => useChatDebugTranscript('a'), { wrapper });
    await waitFor(() => expect(result.current.data?.next_cursor).toBe(1));
    act(() => useChatStreamStore.getState().beginTurn('a', ''));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
    expect(fetch).toHaveBeenCalledTimes(1);
    act(() => useChatStreamStore.getState().markStarted('t1', 'a'));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.data?.next_cursor).toBe(2));
    act(() => useChatStreamStore.getState().appendChunk({ role: 'assistant', content: 'streaming token' }, 'a'));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
    expect(fetch).toHaveBeenCalledTimes(2);
    act(() => useChatStreamStore.getState().finishProjection('a', 't1'));
    await waitFor(() => expect(result.current.data?.next_cursor).toBe(3));
    unmount(); client.clear();
  });
  it('loads every cursor page and refreshes only newer messages', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(Response.json(page('a', 1, true))).mockResolvedValueOnce(Response.json(page('a', 2)));
    vi.stubGlobal('fetch', fetch);
    const signal = new AbortController().signal;
    const initial = await readDebugIncrement('a', undefined, signal);
    expect(initial.messages.map(m => m.cursor)).toEqual([1, 2]);
    expect(fetch.mock.calls[0][0]).toContain('after_id=0');
    expect(fetch.mock.calls[1][0]).toContain('after_id=1');
    expect(fetch.mock.calls[1][0]).toContain('include_artifacts=false');
    fetch.mockResolvedValueOnce(Response.json(page('a', 3)));
    const refreshed = await readDebugIncrement('a', initial, signal);
    expect(fetch.mock.calls[2][0]).toContain('after_id=2');
    expect(refreshed.messages.map(m => m.cursor)).toEqual([1, 2, 3]);
    expect(refreshed.artifacts[0].artifact).toEqual({ props: { version: 'v1.sv3' } });
    expect(refreshed.turns[0].status).toBe('completed');
  });

  it('rejects foreign-chat responses and non-advancing pagination', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(Response.json(page('b', 1))).mockResolvedValueOnce(Response.json({ ...page('a', 0), has_more: true }));
    vi.stubGlobal('fetch', fetch);
    await expect(readDebugIncrement('a', undefined, new AbortController().signal)).rejects.toThrow('Invalid');
    await expect(readDebugIncrement('a', undefined, new AbortController().signal)).rejects.toThrow('advance');
  });

  it('refreshes artifact interaction results even when no messages were added', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(Response.json(page('a', 1)));
    vi.stubGlobal('fetch', fetch);
    const signal = new AbortController().signal;
    const initial = await readDebugIncrement('a', undefined, signal);
    const updated = {
      artifact_id: 'artifact',
      artifact: {
        props: { version: 'v1.sv3' },
        widget_state: { selected: ['option-2'] },
        interaction_state: { status: 'submitted', result: { selected: ['option-2'] } },
      },
    };
    fetch.mockResolvedValueOnce(Response.json({
      ...page('a', 1), messages: [], artifacts: [updated],
    }));
    const refreshed = await readDebugIncrement('a', initial, signal);
    expect(refreshed.messages).toEqual(initial.messages);
    expect(refreshed.next_cursor).toBe(1);
    expect(refreshed.artifacts).toEqual([updated]);
    expect(initial.artifacts).not.toEqual([updated]);
  });

  it('retains loaded messages on failure and never borrows sibling content', async () => {
    const fetch = vi.fn(async (url: string) => Response.json(page(url.includes('/a/') ? 'a' : 'b', 1)));
    vi.stubGlobal('fetch', fetch);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result, rerender, unmount } = renderHook(({ chat }) => useChatDebugTranscript(chat), { wrapper, initialProps: { chat: 'a' } });
    await waitFor(() => expect(result.current.data?.chat_id).toBe('a'));
    fetch.mockRejectedValueOnce(new Error('offline'));
    await result.current.refetch();
    expect(result.current.data?.messages).toHaveLength(1);
    rerender({ chat: 'b' });
    expect(result.current.data?.chat_id).not.toBe('a');
    await waitFor(() => expect(result.current.data?.chat_id).toBe('b'));
    expect(fetch.mock.calls.every(([url]) => !url.includes('/vfs'))).toBe(true);
    unmount(); client.clear();
  });
});
